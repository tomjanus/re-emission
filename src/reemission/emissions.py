"""Classes for calculating GHG emissions from reservoirs.

The net GHG emission is meant to represent the actual emissions exclusively
attributable to the reservoir impoundment and are calculated as follows:
Net GHG emission = (
    Post-ipmoundment balance from the catchment -
    Pre-impoundment balance from the catchment -
    Emissions from the reservoir due to unrelated anthropogenic sources
)
The emissions are calculated for the complete life time of reservoirs that is
assumed = 100 years.

Contains:
    Emission base class from which all other emission classes are derived.
    CarbonDioxideEmission for calculating CO2 emissions.
    MethaneEmission for calculating CH4 emissions.
    NitrousOxideEmission for calculating N2O emissions.

Notes:
    Equations implemented in the methods in the below classes are the same
    equations introduded by Praire et al. 2021. and share the same equation
    references.

    @article{Praire2021,
    title = {A new modelling framework to assess biogenic GHG emissions from reservoirs: The G-res tool},
    journal = {Environmental Modelling & Software},
    volume = {143},
    pages = {105117},
    year = {2021},
    issn = {1364-8152},
    doi = {https://doi.org/10.1016/j.envsoft.2021.105117},
    url = {https://www.sciencedirect.com/science/article/pii/S1364815221001602},
    author = {Yves T. Prairie and Sara Mercier-Blais and John A. Harrison and Cynthia Soued and Paul del Giorgio and Atle Harby and Jukka Alm and Vincent Chanudet and Roy Nahas}
    }
"""
import os
import math
import logging
import configparser
from dataclasses import dataclass
from types import SimpleNamespace
from typing import List, Tuple, Optional, ClassVar
from abc import ABC, abstractmethod
import numpy as np
from reemission.utils import read_config, read_table
from reemission.constants import Landuse
from reemission.catchment import Catchment
from reemission.reservoir import Reservoir
from reemission.temperature import MonthlyTemperature
from reemission.exceptions import WrongN2OModelError

# Get relative imports to data
MODULE_DIR = os.path.dirname(__file__)
INI_FILE = os.path.abspath(os.path.join(MODULE_DIR, 'config', 'config.ini'))
TABLES = os.path.abspath(os.path.join(MODULE_DIR, 'parameters'))

# Set up module logger
logging.basicConfig(level=logging.INFO)
log = logging.getLogger(__name__)


@dataclass  # type: ignore
class Emission(ABC):
    """Abstract base class for all emissions.

    Attributes:
        catchment: Catchment object with catchment data and methods.
        reservoir: Reservoir object with reservoir data and methods.
        preinund_area: Pre-inundation area of a reservoir, [ha].
        config: ConfigParser object of `.ini` file with equation constants.

    Notes:
        Define two generic methods that must be used in all emission subclasses:
        - `profile` for calculating emission decay over a set of years.
        - `factor` for calculating total emission over a life-span.
    """
    catchment: Catchment
    reservoir: Reservoir
    preinund_area: float
    config: configparser.ConfigParser

    def __init__(self, catchment, reservoir, preinund_area=None,
                 config_file=INI_FILE):
        self.catchment = catchment
        self.reservoir = reservoir
        self.config = read_config(config_file)
        if preinund_area is None:
            self.preinund_area = self.catchment.river_area_before_impoundment()
        # Check if preinindation area is not larger than reservoir area
        if self.preinund_area > self.reservoir.area:
            log.warning("Pre impoundment area larger than the reservoir area.")

    def _par_from_config(
            self, list_of_constants: list,
            section_name: str) -> SimpleNamespace:
        """Reads constants (parameters) defined in argument list_of_constants
        from the section of the config file defined in argument section_name.
        """
        const_dict = {par_name: self.config.getfloat(section_name, par_name)
                      for par_name in list_of_constants}
        return SimpleNamespace(**const_dict)

    @abstractmethod
    def profile(self, years: Tuple[int]) -> List[float]:
        """Calculates emission profile in g CO2eq m-2 yr-1 over several years.
        """

    @abstractmethod
    def factor(self, number_of_years: int) -> float:
        """Calculates total emission (factor).
        Returns emissions per m2 of the reservoir per year over the life-span
        of the reservoir, in gCO2,eq / m2 / year.
        """

    @abstractmethod
    def total_emission_per_year(self, number_of_years: int) -> float:
        """Calculates total reservoir emission per year over the life-span
        of the reservoir, in tCO2,eq / year."""

    @abstractmethod
    def total_lifetime_emission(self, number_of_years: int) -> float:
        """Calculates total reservoir emission per life-time in tCO2,eq."""


@dataclass
class CarbonDioxideEmission(Emission):
    """Class for calculating CO2 emissions.

    Attrributes:
        eff_temp: Effective temperature for CO2.
        p_calc_method: Method used for calculating annual discharge of P from
            catchment to the reservoir.
        par: Indexable structure of equation parameters for emission
            calculations.
        pre_impoundment_table: Dictionary of pre_impoundment emission factors.

    Notes:
        Total CO2 emission =
            Diffusive CO2 emission (gross total post-impoundment)+
            - Pre-impoundment emission +
            - Unrelated non-anthropogenic emission
    """

    eff_temp: float
    p_calc_method: str
    par: SimpleNamespace
    pre_impoundment_table: dict

    def __init__(self, catchment, reservoir, eff_temp, p_calc_method,
                 preinund_area=None, config_file=INI_FILE):
        super().__init__(catchment=catchment, reservoir=reservoir,
                         config_file=config_file, preinund_area=preinund_area)
        # Initialise input data specific to carbon dioxide emissions
        self.eff_temp = eff_temp  # EFF temp CO2
        avail_p_calc_methods = ('g-res', 'mcdowell')
        if p_calc_method not in avail_p_calc_methods:
            p_calc_method = 'g-res'
            log.warning(
                "Invalid P calculation method. Expected: %s. " +
                "Using default g-res method.",
                ', '.join(avail_p_calc_methods))
        self.p_calc_method = p_calc_method
        # Read equation parameters an the pre-impoundment table
        self.par = self._par_from_config(
            list_of_constants=['k1_diff', 'k2_diff', 'k3_diff', 'k4_diff',
                               'k5_diff', 'k6_diff', 'k7_diff', 'conv_coeff',
                               'co2_gwp100', 'weight_C', 'weight_CO2'],
            section_name='CARBON_DIOXIDE')
        self.pre_impoundment_table = read_table(
            os.path.join(TABLES, 'Carbon_Dioxide', 'pre-impoundment.yaml'))

    @property
    def reservoir_tp(self) -> float:
        """Return reservoir total phosphorus concentration in micrograms/L."""
        return self.reservoir.reservoir_tp(
            inflow_conc=self.catchment.inflow_p_conc(method=self.p_calc_method),
            method=self.config['CALCULATIONS']['ret_coeff_method'])

    def pre_impoundment(self) -> float:
        """
        Calculate CO2 emissions from the inundated area prior to impoundment.

        Uses a table of pre-impoundment emissions per land cover category
        and soil type, in tCO2-C/ha/yr.

        Returns:
            Unit pre-impoundment emission in g CO2eq m-2 yr-1
        """
        _list_of_landuses = 3 * list(Landuse.__dict__['_member_map_'].values())
        climate = self.catchment.biogenic_factors.climate
        soil_type = self.catchment.biogenic_factors.soil_type
        # Find which landuses are supported from the first entry of pre-impoundment table
        supported_landuses = self.pre_impoundment_table['boreal']['mineral'].keys()
        emissions: List[float] = []
        for landuse, fraction in zip( _list_of_landuses, self.reservoir.area_fractions):
            # Area in ha allocated to each landuse (reservoir.area in km2)
            area_landuse = 100 * self.reservoir.area * fraction
            if landuse.value not in supported_landuses:
                continue
            coeff = self.pre_impoundment_table[climate.value][soil_type.value][landuse.value]
            emissions.append(area_landuse * coeff)
        # Total emission in t CO2-C /yr
        tot_emission = sum(emissions)
        # Total emission in g CO2eq m-2 yr-1. To convert from gCO2-C to gCO2
        # The unit emission is divided by C to CO2 molecular weight ratio, i.e.
        # 44/12
        c_co2_ratio = self.par.weight_C/self.par.weight_CO2
        return tot_emission / self.reservoir.area * 1/c_co2_ratio * \
            self.par.co2_gwp100

    def diffusion_flux(self, year: int, time_horizon: int = 100) -> float:
        r"""
        .. role:: raw-math(raw)
            :format: latex html
        Calculates CO2 diffusive flux for a given year/age in years.
        Return the diffusive flux value in g CO2eq m-2 yr-1

        .. math::
            :nowrap:
            \begin{eqnarray}
            q_{CO_2, diffusion} (t, n) & = & 10^\left( k_1^{diff} + k_2^{diff} \, \log_{10} (t) + k_3^{diff} \, T_{eff,CO_2} + k_4^{diff} \, \log_{10} (A_{res}) +  k_5^{diff} \, m_{sc} + k_6^{diff} \, \log_{10} (C_{TP}) \right) \\
            & & \times \left(1 - \frac{A_{pre}}{A_{res}}\right) \times \frac{44}{12} \times \frac{1}{1000} \times 365 \times gwp_{CO_2}^{n}
            \end{eqnarray}
        where:
            :raw-math:`$t$` is the reservoir age, years
            :raw-math:`$n$` is the time horizon for calculating GWP value, years
            :raw-math:`$T_{eff,CO_2}$` is the effective temperature for CO2, degC
            :raw-math:`$A_{res}$` is the reservoir area, km$^2$
            :raw-math:`$m_{sc}$` is the mass of C in inundated area, kg/m$^2$
            :raw-math:`$C_{TP}$` is the reservoir Total P conc., $\mu$g/L
            :raw-math:`$A_{pre}$` is the preinundation (river) area, km$^2$
            :raw-math:`44/12` is a molecular weight ratio between CO2 and C
            :raw-math:`365` is used to convert emissions from d-1 to yr-1
            :raw-math:`1/1000` is used to convert the unit from mg to g
            :raw-math:`$gwp_{CO_2}^{n}$` is the global warming potential for CO$_2$ over 100 years

        Note:
            Eq. 7 in Praire2021
            n=169, R2=0.36, RMSE=0.39, Outliers=3
        """
        # Time horizon is required to quantify GWP value, which differs
        # depending on the number of years for which global warming potential
        # is quantified.
        if time_horizon != 100:
            log.warning(
                "Currently, the tool supports time horizon of 100 years only.")
            gwp = self.par.co2_gwp100
        else:
            gwp = self.par.co2_gwp100

        flux = (
            gwp * self.par.weight_CO2/self.par.weight_C/1000*365.25
            * 10.0
            ** (
                self.par.k1_diff
                + math.log10(year) * self.par.k2_diff
                + self.eff_temp * self.par.k3_diff
                + math.log10(self.reservoir.area) * self.par.k4_diff
                + self.reservoir.soil_carbon * self.par.k5_diff
                + math.log10(self.reservoir_tp) * self.par.k6_diff
            )
            * (1 - (self.preinund_area / self.reservoir.area)))
        return flux

    def diffusion_flux_int(self, number_of_years: int = 100) -> float:
        r"""
        .. role:: raw-math(raw)
            :format: latex html
        Calculate gross total CO2 emissions in g CO2eq m-2 yr-1
        from a reservoir integrated over number of years
        (n = 100 years by default).

        .. math::
            :nowrap:
            \begin{eqnarray}
            q_{CO_2, gross} (n) & = & q_{CO_2, diffusion} (t=1, n) \times gwp_{CO_2}^{n} \times \\
            & & \frac{n^{k_2^{diff}+1} - 0.5^{k_2^{diff}+1}}{(k_2^{diff}+1)*(n-0.5)}
            \end{eqnarray}
        where:
            :raw-math:`$n$` is the number of years the emission is sumed up for
            :raw-math:`$gwp_{CO_2}^{n}$` is the global warming potential of CO2 over n years

        Note:
            Eq. 8 in Praire2021
            Currently, integration over 100 years is supported by the tool and
                n=100 years is the default value.
        """
        flux = self.diffusion_flux(year=1, time_horizon=number_of_years) * \
            (number_of_years ** (self.par.k7_diff + 1) -
             0.5 ** (self.par.k7_diff + 1)) / \
            ((self.par.k7_diff + 1) * (number_of_years - 0.5))
        return flux

    def diffusion_flux_nonanthro(self) -> float:
        """Calculate nonanthropogenic CO2 flux as CO2 (diffusive) flux
        after 100 years. It is assumed that all anthropogenic effects
        become null after 100 years and the flux that remains after
        100 years is due to non-anthropogenic sources.
        Return flux in g CO2eq m-2 yr-1.
        """
        return self.diffusion_flux(year=100)

    def _diffusion_flux_profile(
            self,
            years: Tuple[int, ...] = (1, 5, 10, 20, 30, 40, 50, 100)) \
            -> list:
        """Calculate CO2 fluxes for a tuple of years given."""
        return [self.diffusion_flux(year) for year in years]

    def net_total(self, number_of_years: int = 100) -> float:
        """
        Calculate net total CO2 emissions, i.e. gross - non anthropogenic
        (in g CO2eq m-2 yr-1) from a reservoir over a number of years
        given in argument `number_of_years`.
        """
        return self.diffusion_flux_int(number_of_years=number_of_years) - \
            self.diffusion_flux_nonanthro()

    def profile(self,
                years: Tuple[int, ...] = (1, 5, 10, 20, 30, 40, 50, 100)) \
            -> List[float]:
        """Calculate CO2 emissions for a list of years given as an argument
        Flux at year x age - pre-impoundment emissions - non-anthropogenic
        emissions, unit: g CO2eq m-2 yr-1."""
        pre_impoundment = self.pre_impoundment()
        non_anthro = self.diffusion_flux_nonanthro()
        diffusion_flux_profile = self._diffusion_flux_profile(years)
        out_profile = [flux - non_anthro - pre_impoundment for
                       flux in diffusion_flux_profile]
        return out_profile

    def factor(self, number_of_years: int = 100) -> float:
        """Overall integrated emissions for lifetime, taken by default
        as 100 yrs, unit: g CO2eq m-2 yr-1"""
        net_total_emission = self.net_total(number_of_years=number_of_years)
        pre_impoundment_emission = self.pre_impoundment()
        return net_total_emission - pre_impoundment_emission

    def total_emission_per_year(self, number_of_years: int = 100) -> float:
        """Calculates total reservoir emission per year in tCO2,eq / year."""
        return self.factor(number_of_years=number_of_years) * \
            self.reservoir.area

    def total_lifetime_emission(self, number_of_years: int = 100) -> float:
        """Calculates total reservoir emission per lifetime in ktCO2,eq."""
        return self.total_emission_per_year(
            number_of_years=number_of_years) * number_of_years / 1_000


@dataclass
class MethaneEmission(Emission):
    """Class for calculating methane emissions from reservoirs.

    Atrributes:
        monthly_temp: temperature.MonthlyTemperature object.
        mean_ir: mean infrared raiation in kWh/m2/d
        pre_impoundment_table: dictionary of parameters for calculating
            pre-impoundment CH4 emissions. None, if table could not be loaded.
        par: subset of parameters relate to CH4 emissions found in `config.ini`

    Gross total CH4 emission (in gCO2eq/m2/year is calculated as a sum of the
        following pathways/processes:
        * CH4 diffusive emissions.
        * CH4 bubbling emission (ebullition).
        * CH4 degassing emissions.
    All of the three above processes are integrated over 100 years.

    Total CH4 emission = Gross total CH4 emission (post-impoundment)+
                         - Pre-impoundment emission +
                         - Unrelated non-anthropogenic emission
    """

    monthly_temp: MonthlyTemperature

    def __init__(self, catchment, reservoir, monthly_temp,
                 preinund_area=None, config_file=INI_FILE):
        self.monthly_temp = monthly_temp
        self.pre_impoundment_table: dict = read_table(
            os.path.join(TABLES, 'Methane', 'pre-impoundment.yaml'))
        super().__init__(
            catchment=catchment,
            reservoir=reservoir,
            config_file=config_file,
            preinund_area=preinund_area)
        # List of parameters required for CH4 emission calculations
        par_list = ['k1_diff', 'k2_diff', 'k3_diff', 'k4_diff',
                    'k1_ebull', 'k2_ebull', 'k3_ebull', 'k1_degas',
                    'k2_degas', 'k3_degas', 'k4_degas', 'weight_CO2',
                    'weight_CH4', 'weight_C', 'ch4_gwp100', 'conv_coeff']
        # Read the parameters from config
        self.par = self._par_from_config(
            list_of_constants=par_list, section_name='METHANE')

    def pre_impoundment(self) -> float:
        """
        Calculate CH4 emissions from the inundated area prior to impoundment.

        Uses a table of pre-impoundment emissions per land cover category
        and soil type, in kgCH4/ha/yr.

        Adds pre-impoundment emission from water bodies prior to impoundment.

        Pre-impoundment emissions are subtracted from the total CH4
        emission, comprised of the sum of degassing, ebullition and
        diffusion emission estimates (as CO2 equivalents).

        Returns:
            Unit pre-impoundment emission in gCO2eq m-2 yr-1
        """
        _list_of_landuses = 3 * list(Landuse.__dict__['_member_map_'].values())
        climate = self.catchment.biogenic_factors.climate
        soil_type = self.catchment.biogenic_factors.soil_type
        supported_landuses = self.pre_impoundment_table['boreal']['mineral'].keys()
        emissions: List[float] = []
        for landuse, fraction in zip(
                _list_of_landuses, self.reservoir.area_fractions):
            # Area in ha allocated to each landuse (reservoir.area in km2)
            area_landuse = 100 * self.reservoir.area * fraction
            if landuse.value not in supported_landuses:
                continue
            coeff = self.pre_impoundment_table[climate.value][soil_type.value][landuse.value]
            # Create a list of emissions per area fraction, in kg CH4 yr-1
            emissions.append(area_landuse * coeff)
            print(soil_type.value, coeff)
        # The below calculation assumes that the windspeed provided as an
        # Attribute to the reservoir object is at 50m height. Put this in
        # documentation or allow wind speed at different heights and addd
        # one more argument which is the windspeed measurement height.
        emissions.append(
            self.reservoir.ch4_preemission_factor() * self.reservoir.area *
            100)
        # Total emission needs to be in g CO2eq m-2 yr-1.
        # To convert from CH4 to CO2
        # the unit emission is multiplied by 44/16, i.e. molecular weight
        # of CO2 divided by molecular weight of CH4.
        # To convert to CO2,eq the value is additionally multiplied by
        # the Global Warming Potential of CH4 over the reservoir's lifespan
        # (100 years). Factor of 1/1000 convert the unit from kg/km2 to g/m2.
        ch4_co2_ratio = self.par.weight_CH4 / self.par.weight_CO2
        tot_emission = sum(emissions) / self.reservoir.area * 1e-3 * \
            1/ch4_co2_ratio * self.par.ch4_gwp100
        return tot_emission

    def ebullition_flux(self, year: Optional[int] = None,
                        time_horizon: int = 100) -> float:
        r"""
        .. role:: raw-math(raw)
            :format: latex html
        Calculate CH4 emission in gCO2eq m-2 yr-1 through ebullition
        (bubbling) using G-Res CH4 Bubbling Emissions equation.
        Calculate for a life-span of n years define in argument `time_horizon'.
        Currently, assumes life-span of 100 years. Eq. 5 in Praire2021.

        .. math::
            :nowrap:
            \begin{eqnarray}
            q_{CH_4, bubbling} (n) & = & 10^\left( k_1^{ebull} + k_2^{ebull} \, \log_{10} (f_{littoral}/100) + k_3^{ebull} \, irr_{mean} \right) \\
            & & \times (365/1000) \, (16/12) \, gwp_{CH_4}^{n}
            \end{eqnarray}
            where:
                :raw-math:`$q_{CH_4, bubbling}$` is CH$_4$ emission via bubbling, (g CO$_{2eq}$ m$^{-2}$ yr$^{-1}$)
                :raw-math:`$f_{littoral}$` is a littoral fraction, (\%)
                :raw-math:`$k_1^{ebull}, k_2^{ebull}, k_3^{ebull}$` are regression coefficients.
                :raw-math:`$gwp_{CH_4}^{n}$` is methane's Global Warming Potential over a 100 year period.
                :raw-math:`$16/12$` is a molecular weight ratio between CH$_4$ and C.
                :raw-math:`$irr_{mean}$` is reservoir's cumulative mean horizontal radiance in kWh/m2/d.
            The value is divided by 1000 in order to convert from mg CO$_{2,eq}$ to g CO$_{2,eq}$.

        Notes:
            Ebullition fluxes are not time-dependent, hence no emission profile
                is calculated.
            Regression statistics: n = 46, R2 = 0.26, RMSE = 0.8, Outlier = 3
            In case other life-spans are to be investigated, the global warming
                potential of CH4 needs to be adjusted to a differnet number of
                years.
        """
        # Time horizon is required to quantify GWP value, which differs
        # depending on the number of years for which global warming potential
        # is quantified.
        if time_horizon != 100:
            log.warning("Currently, the tool supports time horizon of 100 years only.")
            gwp = self.par.ch4_gwp100
        else:
            gwp = self.par.ch4_gwp100
        # Check if the user supplied year in the arguments
        if year is not None:
            log.info(
                "Ebullition is not time-dependent. year argument takes no effect.")
        # Percentage of surface area that is littoral (near the shore)
        littoral_perc = self.reservoir.littoral_area_frac()
        # Calculate CH4 emission in mg CH4-C m-2 d-1
        emission_in_ch4 = 10 ** (
            self.par.k1_ebull
            + self.par.k2_ebull * math.log10(littoral_perc / 100.0)
            + self.par.k3_ebull * self.reservoir.global_radiance())
        # Convert CH4 emission from mg CH4-C m-2 d-1 to g CO2eq m-2 yr-1
        co2_c_ratio = self.par.weight_CH4 / self.par.weight_C
        emission_in_co2 = emission_in_ch4 * 365 * co2_c_ratio * gwp * 1/1000
        return emission_in_co2

    def ebullition_flux_int(self, time_horizon: int = 100) -> float:
        """
        .. role:: raw-math(raw)
            :format: latex html
        Calls ebullition flux. Since ebullition flux is not time-depenent,
        average ebullition flux per year is equal to ebullition flux (at any
        given time).

        .. math::
            :nowrap:
            \begin{equation}
                q_{CH_4, bubbling}^{gross} (n) = q_{CH_4, bubbling} (t=1, n)
            \end{equation}
            since:
            :raw-math:`$q_{CH_4, bubbling}$` is not time-dependent
        """
        return self.ebullition_flux(time_horizon=time_horizon)

    def _ebullition_flux_profile(
            self,
            years: Tuple[int, ...] = (1, 5, 10, 20, 30, 40, 50, 100)) \
            -> List[float]:
        """Converts ebullition emission into a profile with points (emission
        values) define in the argument `years`. Since ebullition is not time
        dependent, the output will be a list with values all equal to the
        ebullition value (scalar).
        """
        return [self.ebullition_flux_int()] * len(years)

    def diffusion_flux(self, year: float, time_horizon: int = 100) -> float:
        r"""
        .. role:: raw-math(raw)
            :format: latex html
        Calculate CH4 emission via diffusion.

        Returns diffusion flux in gCO2eq m-2 yr-1 for a given year.
        Time horizon is used to select the appropriate GWP value.
        Currently, only the time horizon of 100 years is supported.

        Eq. 3 in Praire2021, R2=0.51, RMSE=0.52, N=160

        .. math::
            :nowrap:
            \begin{eqnarray}
            q_{CH_4, diffusion} (t, n)& = & 10^\left( k_1^{diff} + k_2^{diff} \, t + k_3^{diff} \log10\left(\frac{f_{littoral}}{100}\right) + k_4^{diff} T_{eff}^{CH_4} \right) \\
            & & \times (365/1000) \, (16/12) \, gwp_{CH_4}^{n}
            \end{eqnarray}
        where:
            :raw-math:`$q_{CH_4, diffusion}$` is CH$_4$ emission via diffusion, (g CO$_{2eq}$ m$^{-2}$ yr$^{-1}$)
            :raw-math:`$f_{littoral}$` is a littoral fraction, (\%)
            :raw-math:`$n$` is the time horizon in years use to set GWP value
            :raw-math:`$t$` is the reservoir age (after impoundment) in years
            :raw-math:`$k_1^{diff}, k_2^{diff}, k_3^{diff}, k_4^{diff}$` are regression coefficients.
            :raw-math:`$gwp_{CH_4}^{n}$` is methane's Global Warming Potential over a n year horizon.
            :raw-math:`$16/12$` is a molecular weight ratio between CH$_4$ and C.
            :raw-math:`$T_{eff}^{CH_4}$` is the effective temperature for CH$_4$ in ^o$C.
        The value is divided by 1000 in order to convert from mg CO$_{2,eq}$ to g CO$_{2,eq}$.
        """
        # Time horizon is required to quantify GWP value, which differs
        # depending on the number of years for which global warming potential
        # is quantified.
        if time_horizon != 100:
            log.warning("Currently, the tool supports time horizon of 100 years only.")
            gwp = self.par.ch4_gwp100
        else:
            gwp = self.par.ch4_gwp100
        # Percentage of surface area that is littoral (near the shore)
        littoral_perc = self.reservoir.littoral_area_frac()
        # Calculate effective annual temperature for CH4
        eff_temp = self.monthly_temp.eff_temp(gas='ch4')
        # Calculate flux in gCO2eq/m2/yr
        aux_var_1 = self.par.k2_diff * year
        aux_var_2 = self.par.k3_diff * math.log10(littoral_perc / 100.0)
        aux_var_3 = self.par.k4_diff * eff_temp
        flux = 10**(self.par.k1_diff + aux_var_1 + aux_var_2 + aux_var_3) * \
            self.par.weight_CH4/self.par.weight_C * gwp * 365 / 1000
        return flux

    def diffusion_flux_int(self, time_horizon: int = 100) -> float:
        r"""
        .. role:: raw-math(raw)
            :format: latex html
        Calculate integrated unit (peryear) CH4 emission via diffusion.

        The emission is given in gCO2eq m-2 yr-1. Default time horizon of
        100 years is used. The time horizon is required for finding the global
        warming potential which itself depends on the number of years it's
        calculated for.

        Eq. 4 in Praire2021.

        .. math::
            :nowrap:
            \begin{equation}
            q_{CH_4, diffusion}^{gross} (n) = q_{CH_4, diffusion}(t=1, n) \, \frac{1-10^{(100 \, k_2^{diff})}}{-100\,\ln(10)\,k_2^{diff}}
            \end{equation}
        where:
            :raw-math:`$q_{CH_4, diffusion}$` is CH$_4$ emission via diffusion, (g CO$_{2eq}$ m$^{-2}$ yr$^{-1}$)
            :raw-math:`$q_{CH_4, diffusion}^{gross}$` is the unit (per time) gross CH$_4$ emission via diffusion integrated over n=100 years, (g CO$_{2eq}$ m$^{-2}$ yr$^{-1}$)
            :raw-math:`$t$` is the reservoir age (after impoundment) in years
            :raw-math:`$k_2^{diff}$` is a regression coefficients.
        """
        aux1 = self.diffusion_flux(year=1, time_horizon=time_horizon)
        flux = aux1 * (1 - 10**(self.par.k2_diff*time_horizon)) / \
            (-self.par.k2_diff*time_horizon*math.log(10))
        return flux

    def _diffusion_flux_profile(
            self,
            years: Tuple[int, ...] = (1, 5, 10, 20, 30, 40, 50, 100)) \
            -> List[float]:
        """Calculate CH4 emission profile for a vector of years."""
        profile = [self.diffusion_flux(year) for year in years]
        return profile

    def degassing_flux_int(self, time_horizon: int = 100) -> float:
        r"""
        .. role:: raw-math(raw)
            :format: latex html
        Calculates CH4 emission per year via degassing, integreted over time
        horizon given in argumetn `time_horizon` in years.

        Degassing emissions are computed when the hydroleclectric facility has
        a deep water draw off point & when this deep water draw off takes water
        from below the thermocline of a stratified system. For this reason,
        deep water draw off depth and thermocline depth are required for formal
        assesment of whether a degassing flux should be estimated. In general,
        neither of these two data can be reliably measured/estimated. However,
        we can assume that most new hydroelectric facilities will operate deep
        water draw offs, and at least in the tropics in deeper systems
        (>10m mean depth), stratification will occur.

        Equation 6 in Praire2021, R2=0.68, RMSE=0.81, N=38

        Returns:
            Degassing flux in g CO2eq m-2 yr-1

        If water intake depth < thermocline depth: ::math:: $q_{CH_4, degassing}^{gross} = 0$
        Else:
        .. math::
            :nowrap:
            \begin{eqnarray}
            q_{CH_4, degassing}^{gross} (n)& = & 10^{\left(k_1^{degas} + k_2^{degas}\,\log10(WRT) + k_3^{degas}\,\log10\left(q_{CH_4, diffusion}^{gross} (n)\right)\right)} \\
            & & \times q_{dis} \, A_{res} \, gwp_{CH_4}^{n} \, 16/12 \times 0.9 \times 10^{-6}
            \end{eqnarray}
        where:
            :raw-math:`$q_{CH_4, degassing}^{gross}$` is the unit (per time) gross CH$_4$ emission via degassing integrated over n=100 years, (g CO$_{2eq}$ m$^{-2}$ yr$^{-1}$)
            :raw-math:`$q_{CH_4, diffusion}^{gross}$` is the unit (per time) gross CH$_4$ emission via diffusion integrated over n=100 years, (g CO$_{2eq}$ m$^{-2}$ yr$^{-1}$)
            :raw-math:`$k_1^{degas}, k_2^{degas}, k_3^{degas}$` are regression coefficients.
            :raw-math:`$WRT$` is the water residence time in the reservoir, years.
            :raw-math:`$gwp_{CH_4}^{n}$` is methane's Global Warming Potential over a n year horizon.
            :raw-math:`$16/12$` is a molecular weight ratio between CH$_4$ and C.
            :raw-math:`$q_{dis}}$` is the reservoir discharge flow in m$^3$/year.
            :raw-math:`$A_{res}$` is the reservoir surface area in km$^2$.
        """
        # Time horizon is required to quantify GWP value, which differs
        # depending on the number of years for which global warming potential
        # is quantified.
        if time_horizon != 100:
            log.warning("Currently, the tool supports time horizon of 100 years only.")
            gwp = self.par.ch4_gwp100
        else:
            gwp = self.par.ch4_gwp100

        if self.reservoir.water_intake_depth > \
                self.reservoir.thermocline_depth(
                wind_speed=self.reservoir.mean_monthly_windspeed):
            # CH4 conc. difference in mg CH4-C L^(-1) (or gCH4-C m^(-3))
            ch4_conc_diff = 10 ** (
                self.par.k1_degas
                + self.par.k2_degas *
                math.log10(self.reservoir.residence_time)
                + self.par.k3_degas *
                math.log10(self.diffusion_flux_int(time_horizon)))
            # CH4 outflow flux in t CH4-C yr-1
            ch4_out_flux = 0.9 * 1e-6 * ch4_conc_diff * \
                self.reservoir.discharge
            # Degassing flux in gCO2,eq / m2 / year
            return ch4_out_flux * self.par.weight_CH4 / self.par.weight_C * \
                gwp / self.reservoir.area
        return 0.0

    def degassing_flux(self, year: float, time_horizon: int = 100) -> float:
        r"""
        .. role:: raw-math(raw)
            :format: latex html
        Calculate CH4 emission flux via degassing for a given year and
        time horizon. Time horizon is used to select the appropriate GWP value
        for methane.

        The degassing emission flux is back-calculated from the gross
        (integrated) degassing flux and is given in g CO2eq m-2 yr-1.
        .. math::
            :nowrap:
            \begin{equation}
                q_{CH_4, degassing} (t, n) = q_{CH_4, degassing}^{init} (n) \; \exp(-k_4^{degas} \ln(10) \, t)
            \end{equation}
            \begin{equation}
                q_{CH_4, degassing}^{init} (n) = q_{CH_4, degassing}^{gross} (n) * \frac{-k_4^{degas}\,\ln(10)\,n}{1-10^{k_4^{degas}\,n}}
            \end{equation}
        where:
            :raw-math:`$q_{CH_4, degassing} (t, n)$` is the unit (per time) CH$_4$ emission via degassing at time t (years), (g CO$_{2eq}$ m$^{-2}$ yr$^{-1}$)
            :raw-math:`$q_{CH_4, degassing}^{init} (n)$` is the unit (per time) CH$_4$ emission via degassing at time t=0 years, (g CO$_{2eq}$ m$^{-2}$ yr$^{-1}$)
            :raw-math:`$q_{CH_4, degassing}^{gross}(n)$` is the unit (per time) gross CH$_4$ emission via degassing integrated over n=100 years, (g CO$_{2eq}$ m$^{-2}$ yr$^{-1}$)
            :raw-math:`$k_4^{degas}\ln(10)$` is the emission decay time-constant, yr$^{-1}$.
        """
        def init_flux() -> float:
            """Calculate initial degassing flux in year 0, g CO2eq m-2 yr-1."""
            flux = self.degassing_flux_int(time_horizon=time_horizon) * \
                (-self.par.k4_degas * math.log(10) * time_horizon) / \
                (1 - 10 ** (time_horizon * self.par.k4_degas))
            return flux
        return init_flux() * math.exp(self.par.k4_degas * math.log(10) * year)

    def _degassing_flux_profile(
            self,
            years: Tuple[int, ...] = (1, 5, 10, 20, 30, 40, 50, 100)) \
            -> List[float]:
        """Calculate degassing profile for a for a vector of years."""
        profile = [self.degassing_flux(year) for year in years]
        return profile

    def factor(self, number_of_years: int = 100) -> float:
        """Return integrated per area CH4 emission in g CO2eq m-2 yr-1"""
        factor = (
            self.diffusion_flux_int(time_horizon=number_of_years)
            + self.ebullition_flux_int(time_horizon=number_of_years)
            + self.degassing_flux_int(time_horizon=number_of_years)
            - self.pre_impoundment())
        return factor

    def emission_factor(self) -> float:
        """Calculate CH4 Emission Factor for Water Bodies in kg CH4/ha/yr"""
        em_factor = self.reservoir.ch4_emission_factor(wind_height=50)
        print(em_factor)
        return em_factor

    def profile(
            self,
            years: Tuple[int, ...] = (1, 5, 10, 20, 30, 40, 50, 100)) \
            -> List[float]:
        """Return emission profile of CH4 in g CO2eq m-2 yr-1"""
        diff_profile = self._diffusion_flux_profile(years=years)
        ebull_profile = self._ebullition_flux_profile(years=years)
        deg_profile = self._degassing_flux_profile(years=years)
        pre_impound_profile = [-self.pre_impoundment() for _ in years]
        tot_prof = np.array(
            [diff_profile, ebull_profile, deg_profile, pre_impound_profile])
        return list(np.sum(tot_prof, axis=0))

    def total_emission_per_year(self, number_of_years: int = 100) -> float:
        """Calculates total reservoir emission per year in tCO2,eq / year."""
        return self.factor(number_of_years=number_of_years) * \
            self.reservoir.area

    def total_lifetime_emission(self, number_of_years: int = 100) -> float:
        """Calculates total reservoir emission per lifetime in ktCO2,eq."""
        return self.total_emission_per_year(
            number_of_years=number_of_years) * number_of_years / 1_000


@dataclass
class NitrousOxideEmission(Emission):
    """Class for calculating NO2 emissions from reservoirs.

    Attrributes:
        available_models: tuple of supporte N2O emission models.
        model: selected N2O emission  model ('model_1', 'model_2').
        p_export_model: Model for calculating P export from catchments
    """

    available_models: ClassVar[Tuple[str, ...]] = ('model_1', 'model_2')
    model: str
    p_export_model: str

    def __init__(self, catchment, reservoir, model, p_export_model,
                 preinund_area=None, config_file=INI_FILE):
        if model not in self.available_models:
            log.warning('Model %s unknown. ', model)
            log.info('Initializing with default model 1')
            model = 'model_1'
        super().__init__(catchment=catchment, reservoir=reservoir,
                         config_file=config_file, preinund_area=preinund_area)
        # List of parameters required for CH4 emission calculations
        par_list = ['nitrous_gwp100', 'weight_O', 'weight_P', 'weight_N']
        # Read the parameters from config
        self.par = self._par_from_config(
            list_of_constants=par_list, section_name='NITROUS_OXIDE')
        self.model = model
        self.p_export_model = p_export_model

    def _total_to_unit(self, emission: float) -> float:
        """Convert emission from kgN yr-1 to mmolN/m^2/yr."""
        return emission / self.par.weight_N / self.reservoir.area

    def _unit_to_total(self, unit_emission: float) -> float:
        """Convert emission from mmolN/m^2/yr to kgN yr-1."""
        return unit_emission * self.reservoir.area * self.par.weight_N

    def tn_fixation_load(self) -> float:
        r"""Total N internal fixation load following the method in
        Maarva et al (2018).

        Total N fixation depends on water residence time in the reservoir
        and molar TN:TP stoichiometry. It is formulated as the \% of the
        riverine inflow TN load using the following formula:

        Total N fixation load [\%]:
        .. math::
            :nowrap:
            \begin{equation}
                L_{TN,fix} = \mu \, \left[ \frac{37.2}{1 + \exp(0.5 * {TN/TP} \, - 6.877)}  \right]
            \end{equation}
        where:
        .. math::
            :nowrap:
            \begin{equation}
                \mu = \textrm{erf} ((WRT - 0.028) / 0.04)
            \end{equation}

        with residence_time (WRT)given in years

        To account for uncertainties in the total N fixation load estimates,
        a normal distribution with standard deviation of +/-10% was assumed
        around the predicted total N fixation load values (Akbarzahdeh 2019)
        """
        tp_load_annual = self.catchment.phosphorus_load(
            method=self.p_export_model)  # kg P / yr
        tn_load_annual = self.catchment.nitrogen_load()  # kg N / yr
        mu_coeff = max(
            0, math.erf((self.reservoir.residence_time - 0.028) / 0.04))
        #  molar ratio of inflow TP and TN loads (-)
        tn_tp_ratio = (tn_load_annual / self.par.weight_N) / \
            (tp_load_annual / self.par.weight_P)
        tn_fix_percent = (
            37.2 / (1 + math.exp(0.5 * tn_tp_ratio - 6.877))) * mu_coeff
        # Calculate total internal N fixation in kg/yr
        return 0.01 * tn_fix_percent * tn_load_annual

    def factor(self, number_of_years: int = 666, mean: bool = False,
               model: Optional[str] = None) -> float:
        """Return N2O emission in gCO2eq/m2/yr. N2O emissions are not
        calculated over a defined time horizon as e.g. CO2. Thus,
        the time horizon for N2O is given the number of the beast"""
        if not model:
            model = self.model
        if model not in self.available_models:
            raise WrongN2OModelError(permitted_models=self.available_models)
        if mean:
            output = 0.5 * (self._n2o_emission_m1_co2() +
                            self._n2o_emission_m2_co2())
        else:
            if model == "model_1":
                output = self._n2o_emission_m1_co2()
            if model == "model_2":
                output = self._n2o_emission_m2_co2()
        return output

    def profile(
            self,
            years: Tuple[int] = (1, 5, 10, 20, 30, 40, 50, 100)) -> \
            List[float]:
        """Return N2O emission profile for the years defined in parameer
        years. Only done for the purpose of keeping consistency with other
        emissions, since N2O does not have an emission profile. Thus,
        the returned profile is a straight line with values equal to
        the N2O emission factor"""
        return [self.factor()] * len(years)

    def _n2o_emission_m1_co2(self) -> float:
        """Calculate N2O emission in gCO2eq m-2 yr-1 according to model 1"""
        # 1. Calculate total N2O emission (kgN yr-1)
        total_n2o_emission = self._n2o_denitrification_m1() + \
            self._n2o_nitrification_m1()
        # 2. Calculate unit total N2O emission in mmolN/m^2/yr
        unit_n2o_emission = self._total_to_unit(total_n2o_emission)
        # 3. Calculate emission in gCO2eq/m2/yr
        total_n2o = self.par.weight_N * \
            (1 + self.par.weight_O / (2 * self.par.weight_N)) * \
            self.par.nitrous_gwp100 * unit_n2o_emission * 10**(-3)
        return total_n2o

    def _n2o_emission_m2_co2(self) -> float:
        """Calculate N2O emission in gCO2eq m-2 yr-1 according to model 2"""
        total_n2o = self.par.weight_N * \
            (1 + self.par.weight_O / (2 * self.par.weight_N)) * \
            self.par.nitrous_gwp100 * self._unit_n2o_emission_m2() * 10**(-3)
        return total_n2o

    def _n2o_denitrification_m1(self) -> float:
        """Calculate N2O emission (kgN yr-1) from denitrification using
        Model 1
        0.009 * [tn_catchment_load + tn_fixation_load] *
            [0.3833 * erf(0.4723 * residence time(yrs))]
        """
        n2o_emission_den = (
            0.009 * (self.catchment.nitrogen_load() + self.tn_fixation_load())
            * (0.3833 * math.erf(0.4723 * self.reservoir.residence_time)))
        return n2o_emission_den

    def _n2o_nitrification_m1(self) -> float:
        """Calculate N2O emission (kgN yr-1) from nitrification using
        Model 1
        0.009 * [tn_catchment_load + tn_fixation_load] *
            [0.5144 * erf(0.3692 * water residence time(yrs))]
        """
        n2o_emission_nitr = (
            0.009 * (self.catchment.nitrogen_load() + self.tn_fixation_load())
            * (0.5144 * math.erf(0.3692 * self.reservoir.residence_time)))
        return n2o_emission_nitr

    def _n2o_emission_m2_n(self) -> float:
        """Calculate total N2O emission (kgN yr-1) using Model 2
        --------------------------------------------------------
        From an overall relation derived from N2O emissions
        computed as the sum of two EF terms: N2O derived from
        denitrification, and N2O derived from Nitrification.
        This approach differs from N2OA above in that the derivation of the
        equation below included mechanisms to account for N2O saturation
        state with respect to gaseous emissions (effectively not all N2O
        produced is assumed to be evaded), and for internal consumption of
        N2O produced by denitrification, which increases as a function of
        water residence time.
        """
        n2o_emission = self.catchment.nitrogen_load() * (
            0.002277 * math.erf(1.63 * self.reservoir.residence_time))
        return n2o_emission

    def _unit_n2o_emission_m2(self) -> float:
        """Calculate unit total N2O emission in mmolN/m^2/yr using Model 2."""
        return self._total_to_unit(self._n2o_emission_m2_n())

    def _n2o_denitrification_m2(self) -> float:
        """Calculate N2O emission from denitrification in [kgN/yr] using
        Model 2.
        """
        # Calculate unit N2O emission from denitfication in mmol N m-2 yr-1
        unit_n2o_denitrification = 0.7789 * math.exp(
            -((self.reservoir.residence_time + 1.366) / 2.751)) ** 2 * \
            self._unit_n2o_emission_m2()
        # Return N2O emission in kgN/yr
        return self._unit_to_total(unit_n2o_denitrification)

    def _n2o_nitrification_m2(self) -> float:
        """Calculate N2O emission from nitrification in [kgN/yr] using
        Model 2
        """
        unit_n2o_nitrification = self._unit_n2o_emission_m2() - \
            self._total_to_unit(self._n2o_denitrification_m2())
        # Return N2O emission in kgN/yr
        return self._unit_to_total(unit_n2o_nitrification)

    # Additional methods calculating effluent nitrogen load and concentration
    # from the reservoir associated with the calculated N2O emission
    def nitrogen_downstream_load(self) -> float:
        """Calculate downstream TN load in [kgN/yr]"""
        # 1. Calculate TN burial as a factor of input TN
        tn_burial_factor = 0.51 * math.erf(
            0.4723 * self.reservoir.residence_time)
        # 2. Calculate TN denitrification as a factor of input TN
        tn_denitr_factor = 0.3833 * math.erf(
            0.4723 * self.reservoir.residence_time)
        # 3. Calculate TN loading (catchment + fixation) in kg N yr-1
        tn_loading = self.catchment.nitrogen_load() + self.tn_fixation_load()
        # 4. Calculate TN burial in kg N yr-1
        tn_burial = tn_burial_factor * tn_loading
        # 5. Calculate TN denitrification in kg N yr-1
        tn_denitr = tn_denitr_factor * tn_loading
        # 6. Calculate TN downstream load in kg N yr-1
        tn_downstream_load = tn_loading - tn_burial - tn_denitr
        return tn_downstream_load

    def nitrogen_downstream_conc(self) -> float:
        """Calculate downstream TN concentration in [mgN/L] == [gN/m3]"""
        return 1e03 * self.nitrogen_downstream_load() / \
            self.catchment.discharge

    def total_emission_per_year(self, number_of_years: int = 100) -> float:
        """Calculates total reservoir emission per year in tCO2,eq / year."""
        return self.factor(number_of_years=number_of_years) * \
            self.reservoir.area

    def total_lifetime_emission(self, number_of_years: int = 100) -> float:
        """Calculates total reservoir emission per lifetime in ktCO2,eq."""
        return self.total_emission_per_year(
            number_of_years=number_of_years) * number_of_years / 1_000
