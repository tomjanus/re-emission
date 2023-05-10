"""Module for processing shape delineation files from HEET

Summary: Delineations produced by HEET come in batches and are saved in multiple
separate folders.

Naming conventions of the shape files produced in reservoir / catchment delineation
    C_{ID} - catchment (MultiPolygon)
    MS_{ID} - flooded river section (MultiLineString)
    PS_{ID} - barrier (Point)
    R_{ID} - reservoir (MultiPolygon)
    N_{ID} - catchment minus reservoir (MultiPolygon)
"""
from __future__ import annotations
from typing import List, Dict, Set, Optional
from dataclasses import dataclass, field
import pathlib
import re
import json
import pandas as pd
import geopandas as gpd
from reemission.utils import get_package_file


@dataclass
class GeoData:
    """Wrapper class for storing and saving geodataframes"""
    dataframe: gpd.GeoDataFrame

    def save(self, folder: pathlib.Path, file_name: str) -> None:
        """Save the geodatafram to one of the geodata formats supported by
        geopandas"""
        pathlib.Path(folder).mkdir(parents=True, exist_ok=True)
        self.dataframe.to_file(pathlib.Path(folder/file_name))


@dataclass
class ShpConcatenator:
    """Tool for finding shape files mathing a provided file (glob) pattern, 
    adding them together and saving into a single shape file."""
    shp_files: List[pathlib.Path] = field(default_factory=list)

    def find_in_folders(
            self, folders: List[pathlib.Path], glob_pattern: str,
            sel_ids: Optional[List[int]] = None) -> Dict:
        """Find shape files in folder that match the pattern given in
        glob_pattern and represent certain dam/reservoir IDs. An example
        glob pattern could be `MS_*.shp` which, in the context of heet, matches
        shape files for any dam/reservoir ID representing flooded river 
        sections"""
        # List all shapes files in folders matching a given pattern
        shp_files = []
        for folder in folders:
            shp_files.extend(list(pathlib.Path(folder).glob(glob_pattern)))
        found: Set[int] = set()
        if sel_ids:
            # Remove duplicates
            sel_ids = list(set(sel_ids))
            selected_shape_files: List[pathlib.Path] = []
            file_endings = ["_" + str(index) + ".shp" for index in sel_ids]
            for index, file_ending in zip(sel_ids, file_endings):
                for shape_file in shp_files:
                    if not shape_file.as_posix().endswith(file_ending):
                        continue
                    found.add(index)
                    selected_shape_files.append(shape_file)
        else:
            sel_ids = []
            selected_shape_files = shp_files
            # Get dam/reservoir index values from file names (By HEET convention
            # delineation shp file has ID embedded in file name.)
            for shp_file in selected_shape_files:
                shp_match = re.search(r'_\d+.shp', shp_file.as_posix())
                if not shp_match:
                    continue
                id_match = re.search(r'\d+', shp_match.group())
                if not id_match:
                    continue
                sel_ids.append(int(id_match.group()))
                found.add(int(id_match.group()))
        # duplicates = {x for x in list(found) if list(found).count(x) > 1}
        # Return output statistics
        self.shp_files = selected_shape_files
        return {
            'selected ids': sel_ids,
            'found ids': list(found),
            'missing ids': list(set(sel_ids) - found)}

    def concatenate(self) -> GeoData:
        """Combine multiple shapes into a single geodataframe"""
        gpd_files = [gpd.read_file(shp_file) for shp_file in self.shp_files]
        gdf = pd.concat(gpd_files).pipe(gpd.GeoDataFrame)
        return GeoData(gdf)


def test_shp_concatenation() -> None:
    """Test shp file concatenation and parsing using demo data"""
    shp_concat = ShpConcatenator()
    shp_dirs_rel = [
        "../../examples/demo/XHEET_23MYEX1-9_20230316-1814/",
        "../../examples/demo/XHEET_23MYFP1-2_20230305-1653/",
        "../../examples/demo/XHEET_23MYFP2-2_20230305-2021/"
    ]
    shp_file_folders = [get_package_file(folder) for folder in shp_dirs_rel]
    stats = shp_concat.find_in_folders(
        folders=shp_file_folders, glob_pattern="R_*.shp")
    print(json.dumps(stats, indent=4))
    g_data = shp_concat.concatenate()
    g_data.save(
        folder=get_package_file("../../input_data"), file_name='test.shp')


if __name__ == "__main__":
    """ """
    test_shp_concatenation()