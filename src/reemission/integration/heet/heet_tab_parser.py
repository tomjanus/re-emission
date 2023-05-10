"""Module for reading, concatenating and filtering tabular outputs from
the HEET reservoir and catchment delineation package.

The purpose of this module is to create a single output .csv file from mulitple 
csv output files generated by HEET (e.g. from multiple analyses of different 
catchments and reservoirs). The file is then parsed, unwanted columsn are
removed whilst columns representing missing data, are added.
"""
from __future__ import annotations
from typing import List, Set, Any, Dict, Optional, TypeVar, Hashable, Generic
import pathlib
from dataclasses import dataclass, field
import math
import pydantic
import pandas as pd
import geopandas as gpd
from reemission.integration.heet.custom_exceptions import \
    ColumnsNotFoundError, FileDoesNotExistError
from reemission.utils import get_package_file, load_toml
from reemission.app_logger import create_logger

# NOTE: "r_msoc_kgperm2" now replaced with r_msocs_kgperm2
# By default output csv file from HEET is called 'output_parameters.csv'
DEFAULT_HEET_OUTPUT_FILE = "output_parameters.csv"

# Create a logger
logger = create_logger(logger_name=__name__)


UUID = TypeVar("UUID", bound=Hashable)


@dataclass
class SuppDataExistingReservoirs:
    """Supplementary data to be provided from other sources for existing
    reservoir delineations.
    Attributes:
        id: unique identified of a data point (reservoir)
        volume: reservoir volume in m3
        max_depth: reservoir maximum depth in metres
    """
    volume: int = field(metadata={'unit': 'm3'})
    max_depth: float = field(metadata={'unit': 'm'})


@dataclass
class SuppDataDB(Generic[UUID]):
    """Container for supplememtary data for reservoirs"""
    key_name: str
    data: Dict[UUID, SuppDataExistingReservoirs]

    
@dataclass
class SuppDataMyanmar(SuppDataDB):
    """Container for supplementary data for Burmese dams"""

    @classmethod
    def from_ifc_db(
            cls, file_name: pathlib.Path, 
            uid_field: str = "IFC_ID") -> SuppDataMyanmar:
        """Obtain supplementary data from IFC database of dams"""
        # Distance between low water level and the bottom of the reservoir
        delta_h: float = 2.0

        # Max depth calculation methods
        def max_h_method_1(row: gpd.GeoSeries) -> float:
            return row["FSL (m)"] - row["LWL (m)"] + delta_h

        def max_h_method_2(row: gpd.GeoSeries) -> float:
            return row['Drawdown'] + delta_h

        def max_h_method_3(row: gpd.GeoSeries) -> float:
            return row['DEPTH_M']

        def calculate_volume(row: gpd.GeoSeries) -> int:
            """Calculate reservoir volume (in m3) rounded up to an integer
            value"""
            volume = row["STOR_MCM"] * 1_000_000
            return int(volume)

        def calculate_max_depth(row: gpd.GeoSeries) -> float:
            """Calculate maximum depth of a reservoir"""
            for max_h_method in [
                    max_h_method_1, max_h_method_2, max_h_method_3]:
                max_depth = max_h_method(row)
                if not math.isnan(max_depth):
                    return max_depth
            return max_depth

        ifc_db = gpd.read_file(file_name)
        data = {}
        for _, row in ifc_db.iterrows():
            uid = row[uid_field]
            volume = calculate_volume(row)
            max_depth = calculate_max_depth(row)
            data[uid] = SuppDataExistingReservoirs(volume, max_depth)
        return cls(key_name=uid_field, data=data)


@dataclass
class TabularHeetOutput:
    """Collection of methods for pre-processing and validating tabular output
    data from HEET prior to conversion into RE-EMISSION input file,"""
    data: pd.DataFrame

    def __add__(self, other: TabularHeetOutput) -> TabularHeetOutput:
        """Addition of parsers by concatenating their dataframe attributes.
        Used for working on multiple dataframes, e.g. tabular outputs from HEET
        from multiple batches of reservoirs."""
        if other.__class__ == self.__class__:
            concat_data = pd.concat([self.data, other.data], axis=0, ignore_index=True)
            return TabularHeetOutput(concat_data)
        return NotImplemented
    
    def set_index(self, col_name: str) -> None:
        """Set index on the dataframe"""
        self.data.set_index(col_name, inplace=True)

    @classmethod
    def from_csv(
            cls, filename: pathlib.Path, 
            id_column: Optional[str] = None) -> TabularHeetOutput:
        """Instantiate object from csv file containing tabular Heet output data
        """
        df_ = pd.read_csv(filename)
        if id_column:
            try:
                df_.set_index(id_column, inplace=True)
            except KeyError:
                pass
        return cls(df_)

    def to_csv(self, filename: pathlib.Path) -> None:
        """Save data to a CSV file"""
        pathlib.Path(filename.parent).mkdir(parents=True, exist_ok=True)
        self.data.to_csv(filename, index=False)

    @property        
    def number_of_rows(self) -> int:
        """Find number of rows in the data attribute"""
        return len(self.data.index)
    
    def list_items(self, field_name: str) -> List[Any]:
        """Convert a column into a list"""
        return list(self.data[field_name])

    def filter_columns(
            self, mandatory_columns: List[str], 
            optional_columns: Optional[List[str]] = None) -> None:
        """Trim the dataframe by keeping all mandatory and optional columns and
        purging the rest.
        Parameters:
            mandatory_columns: List of columns to be retained. All columns should
                be present in the original dataframe.
            optional_columns: List of columns to be retained if the column exists.
        Raises:
            ColumnsNotFoundError: if one or more mandatory columns not found.
        """
        columns: List[str] = list(self.data.columns)
        missing_mandatory: Set[str] = set(mandatory_columns) - set(columns)
        if missing_mandatory:
            raise ColumnsNotFoundError(missing_mandatory)
        retained_columns = mandatory_columns
        if optional_columns:
            missing_optional: Set[str] = set(optional_columns) - set(columns)
            available_optional = set(optional_columns) - missing_optional
            retained_columns += list(available_optional)
        # Trim dataframe so that it only contains the retained columns
        self.data = self.data[retained_columns]

    def add_column(
            self, column_name: str, default_value: Any, 
            replace: bool = False) -> None:
        """Add new column to dataframe and fill the fields with a default
        value (uniform across all rows)"""
        if column_name in self.data.columns and not replace:
            return
        self.data[column_name] = default_value

    def filter_rows(self, on_column: str, kept_values: List[Any]) -> None:
        """Removes rows in the dataframe for which the value in column given
        in attribute `on_column` is not in the list of values given in the
        attribute `kept_values`."""
        logger.info('Filtering data on column "%s"', on_column)
        logger.info("Kept values: %s", ", ".join(list(map(str, kept_values))))
        self.data = self.data[self.data[on_column].isin(kept_values)]
        remaining_values = self.data[on_column]
        logger.info("Values in the filtered dataframe: %s",
                    ", ".join(list(map(str, remaining_values))))
        missing_ids = list(set(kept_values) - set(remaining_values))
        if not missing_ids:
            logger.info('All required values in column "%s" are present.', 
                        on_column)
        else:
            logger.warning(
                "The following column values could not be found in original data: %s",
                ", ".join(list(map(str, missing_ids))))

    def handle_existing_reservoirs(
            self, supp_info: SuppDataDB, on_key: str = "id") -> None:
        """Supplies information from external sources for reservoirs calculated
        using the 'existing' reservoir option. These reservoir delineations
        miss information about:
            - volume [m3], max_depth [m], mean_depth [m]"""
        existing_dam_data = self.data[~self.data['future_dam_model']]
        for ix_, row in existing_dam_data.iterrows():
            dam_id = row[on_key]
            # Find supporting data
            try:
                data = supp_info.data[dam_id]
            except KeyError:
                logger.error("Dam ID %d not found", dam_id)
            else:
                self.data.at[ix_, 'r_volume_m3'] = data.volume
                self.data.at[ix_, 'r_maximum_depth_m'] = data.max_depth
                # TODO: 2 below is a fudge factor because mean depths tended to 
                # be larger than max depths
                self.data.at[ix_, 'r_mean_depth_m'] = \
                    data.volume / row['r_area_km2'] / 1_000_000 / 2

    def remove_duplicates(self, on_column: str, keep: str = "first") -> None:
        """Removes rows with duplicate fields
        Parameters:
            on_column: name of the field/column in which duplicates are sought
            keep: 'first' to keep the first value, 'last' to keep the last value
        """
        orig_df_length = len(self.data)
        self.data.drop_duplicates(subset=on_column, keep=keep, inplace=True)
        new_df_length = len(self.data)
        if orig_df_length - new_df_length > 0:
            logger.info("Removed row duplicates. Dropped %d rows",
                        orig_df_length-new_df_length)


class HeetOutputReader(pydantic.BaseModel):
    """Reads individual tabular outputs from heet and combines the data
    into a single data objct."""
    file_paths: List[pathlib.Path]

    @pydantic.validator("file_paths")
    @classmethod
    def files_exist(cls, value):
        """Ensure that all folders exist and are directories"""
        for file_path in value:
            if not file_path.is_file():
                raise FileDoesNotExistError(
                    file_name=file_path,
                    message=f"File {file_path} does not exist.")
        return value

    def read_files(self) -> TabularHeetOutput:
        """Read files form paths listed in `file_paths` """
        if not isinstance(self.file_paths, list):
            raise TypeError("File names should be a list")
        file_parser = TabularHeetOutput.from_csv(self.file_paths[0])
        # Combine files if more than one is provided
        if len(self.file_paths) > 1:
            for file_path in self.file_paths[1:]:
                file_parser = file_parser + TabularHeetOutput.from_csv(file_path)
        return file_parser


def test_tab_data_parsing() -> None:
    """Parse tabular output data from HEET generated for demo purposes"""
    # Get the IFC database of dams (providing supplementary data)
    ifc_db_file = get_package_file(
        "../../examples/demo/ifc_db/all_dams_replaced_refactored.shp")
    # Get the heet output folders
    heet_output_1 = get_package_file(
        "../../examples/demo/XHEET_23MYEX1-9_20230316-1814/") / \
        DEFAULT_HEET_OUTPUT_FILE
    heet_output_2 = get_package_file(
        "../../examples/demo/XHEET_23MYFP1-2_20230305-1653/") / \
        DEFAULT_HEET_OUTPUT_FILE
    heet_output_3 = get_package_file(
        "../../examples/demo/XHEET_23MYFP2-2_20230305-2021/") / \
        DEFAULT_HEET_OUTPUT_FILE
    # Read the tabular output files
    output_reader = HeetOutputReader(
        file_paths=[heet_output_1, heet_output_2, heet_output_3])
    heet_output = output_reader.read_files()
    heet_output.remove_duplicates(on_column="id")
    # Load supplementary data from the ifc database
    sup_data = SuppDataMyanmar.from_ifc_db(ifc_db_file)
    #heet_output.set_index("id")
    heet_output.handle_existing_reservoirs(sup_data)
    # Get the list of mandatory columns from config file
    tab_data_config = load_toml(
       get_package_file("./config/heet.toml"))['tab_data']
    heet_output.filter_columns(
        mandatory_columns=tab_data_config['mandatory_fields'],
        optional_columns=tab_data_config['unused_inputs'])
    # Add missing columns containing information about treatment factor and
    # landuse intensity that are not currently present in HEET
    heet_output.add_column(
        column_name="c_treatment_factor", default_value="primary (mechanical)")
    heet_output.add_column(
        column_name="c_landuse_intensity", default_value="low intensity")
    # Save the combined and parsed outputs csv file
    heet_output.to_csv(
        get_package_file("../../input_data/all_heet_outputs.csv"))


if __name__ == "__main__":
    test_tab_data_parsing()
