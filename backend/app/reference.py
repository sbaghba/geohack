"""
Reference tables and model assumptions, each with its source. Every number the UI shows traces to here,
to the grid (scripts/build_grid.py), or to a live lookup.
"""
from __future__ import annotations

# ---- EPA eGRID2023 (rev2, data year 2023): total output CO2 emission rate, lb/MWh --------------------
# https://www.epa.gov/system/files/documents/2025-06/summary_tables_rev2.pdf
EGRID_SUBREGION_LB = {"SRVC": 593.4, "SRTV": 898.1}  # most of NC is SRVC (Duke/Dominion); far-west NC is TVA (SRTV)
EGRID_STATE_LB = {
    "AK": 809.8, "AL": 711.0, "AR": 993.4, "AZ": 686.7, "CA": 393.5, "CO": 1085.0, "CT": 538.3, "DC": 394.1,
    "DE": 703.2, "FL": 786.6, "GA": 713.8, "HI": 1385.1, "IA": 630.2, "ID": 313.3, "IL": 471.7, "IN": 1457.2,
    "KS": 728.2, "KY": 1735.0, "LA": 761.4, "MA": 822.4, "MD": 520.0, "ME": 311.8, "MI": 793.2, "MN": 747.5,
    "MO": 1444.1, "MS": 826.3, "MT": 1056.2, "NC": 624.0, "ND": 1288.4, "NE": 1018.2, "NH": 272.9, "NJ": 468.3,
    "NM": 770.2, "NV": 642.4, "NY": 465.1, "OH": 1063.8, "OK": 647.2, "OR": 364.2, "PA": 645.8, "RI": 839.1,
    "SC": 557.2, "SD": 334.0, "TN": 657.3, "TX": 768.3, "UT": 1413.0, "VA": 536.9, "VT": 43.0, "WA": 265.1,
    "WI": 1157.0, "WV": 1954.4, "WY": 1820.3,
}
EGRID_US_LB = 767.2
SRTV_NC_COUNTIES = {"Cherokee", "Clay", "Graham", "Swain"}  # TVA service area in NC (approximate)

# ---- EIA State Electricity Profiles 2024: average retail price, all sectors, cents/kWh -------------------
# https://www.eia.gov/electricity/state/   (industrial rates are lower; this is a conservative upper estimate)
EIA_PRICE_CENTS = {
    "AL": 11.90, "AK": 22.17, "AZ": 12.74, "AR": 9.59, "CA": 27.04, "CO": 12.07, "CT": 24.37, "DE": 13.56,
    "DC": 16.88, "FL": 12.53, "GA": 11.40, "HI": 38.00, "ID": 9.51, "IL": 12.21, "IN": 11.38, "IA": 9.34,
    "KS": 11.21, "KY": 10.07, "LA": 8.80, "ME": 19.66, "MD": 15.04, "MA": 23.94, "MI": 14.16, "MN": 12.35,
    "MS": 10.93, "MO": 11.06, "MT": 10.83, "NE": 9.07, "NV": 11.47, "NH": 20.61, "NJ": 16.29, "NM": 9.18,
    "NY": 19.66, "NC": 11.65, "ND": 7.93, "OH": 11.29, "OK": 9.09, "OR": 11.11, "PA": 12.51, "RI": 24.15,
    "SC": 10.90, "SD": 10.87, "TN": 10.90, "TX": 9.79, "UT": 9.97, "VT": 18.41, "VA": 10.62, "WA": 10.13,
    "WV": 11.05, "WI": 12.72, "WY": 9.14,
}

# ---- NC Department of Revenue: county property tax rates FY2025-26, $ per $100 valuation ---------------
# https://www.ncdor.gov/2025-2026-county-tax-rates-finalpdf/open
NC_COUNTY_TAX = {
    "Alamance": .4940, "Alexander": .6500, "Alleghany": .5970, "Anson": .7770, "Ashe": .4400, "Avery": .4000,
    "Beaufort": .4450, "Bertie": .9300, "Bladen": .7850, "Brunswick": .3420, "Buncombe": .5466, "Burke": .5550,
    "Cabarrus": .5760, "Caldwell": .4975, "Camden": .7300, "Carteret": .2250, "Caswell": .6270, "Catawba": .3985,
    "Chatham": .6000, "Cherokee": .6100, "Chowan": .6950, "Clay": .4300, "Cleveland": .5450, "Columbus": .8050,
    "Craven": .4448, "Cumberland": .4990, "Currituck": .6200, "Dare": .2632, "Davidson": .5400, "Davie": .6486,
    "Duplin": .5800, "Durham": .5542, "Edgecombe": .8900, "Forsyth": .5352, "Franklin": .5050, "Gaston": .5990,
    "Gates": .6700, "Graham": .5900, "Granville": .6310, "Greene": .7860, "Guilford": .7305, "Halifax": .7000,
    "Harnett": .5910, "Haywood": .5500, "Henderson": .4310, "Hertford": .8400, "Hoke": .7300, "Hyde": .9200,
    "Iredell": .5000, "Jackson": .3100, "Johnston": .5200, "Jones": .7400, "Lee": .6500, "Lenoir": .6750,
    "Lincoln": .4990, "Macon": .2700, "Madison": .3600, "Martin": .7200, "McDowell": .5675, "Mecklenburg": .4927,
    "Mitchell": .5600, "Montgomery": .6150, "Moore": .2950, "Nash": .6300, "New Hanover": .3060,
    "Northampton": .8250, "Onslow": .6550, "Orange": .6383, "Pamlico": .6450, "Pasquotank": .6200,
    "Pender": .7375, "Perquimans": .5200, "Person": .6300, "Pitt": .5663, "Polk": .4277, "Randolph": .5000,
    "Richmond": .7300, "Robeson": .7500, "Rockingham": .5801, "Rowan": .5800, "Rutherford": .4540,
    "Sampson": .6850, "Scotland": .9900, "Stanly": .5100, "Stokes": .6250, "Surry": .5130, "Swain": .4100,
    "Transylvania": .4105, "Tyrrell": .8700, "Union": .4342, "Vance": .7129, "Wake": .5171, "Warren": .5796,
    "Washington": .8500, "Watauga": .3180, "Wayne": .6259, "Wilkes": .4200, "Wilson": .5950, "Yadkin": .6500,
    "Yancey": .5200,
}

# ---- Model assumptions (approximate industry figures; cite in README) ---------------------------------
PUE = {"evaporative": 1.2, "air": 1.4, "liquid": 1.15}
WUE_L_PER_KWH = {"evaporative": 1.8, "air": 0.2, "liquid": 0.1}
UTILIZATION = {"ai": 0.8, "mixed": 0.6}
EWIF_L_PER_KWH = 4.5             # off-site water for power generation, US average estimate
HOME_KWH_YR = 10_500             # average US home
HOUSEHOLD_M3_YR = 414.5          # ~300 gal/day household
CAR_T_CO2_YR = 4.6               # typical passenger vehicle (EPA)
ACRES_PER_MW = 0.75
CAPEX_USD_PER_MW = 10e6
CONSTRUCTION_JOBS_PER_MW = 10
PERMANENT_JOBS_PER_MW = 0.5
TAXABLE_SHARE = 0.5              # share of capex on the tax roll after equipment depreciation/incentives
GAS_LB_PER_MWH = 900
SOLAR_OFFSET = 0.05
MIN_GRID_KV = 115                # lines/substations counted as viable interconnection

US_STATE_ABBR = {
    "01": "AL", "02": "AK", "04": "AZ", "05": "AR", "06": "CA", "08": "CO", "09": "CT", "10": "DE", "11": "DC",
    "12": "FL", "13": "GA", "15": "HI", "16": "ID", "17": "IL", "18": "IN", "19": "IA", "20": "KS", "21": "KY",
    "22": "LA", "23": "ME", "24": "MD", "25": "MA", "26": "MI", "27": "MN", "28": "MS", "29": "MO", "30": "MT",
    "31": "NE", "32": "NV", "33": "NH", "34": "NJ", "35": "NM", "36": "NY", "37": "NC", "38": "ND", "39": "OH",
    "40": "OK", "41": "OR", "42": "PA", "44": "RI", "45": "SC", "46": "SD", "47": "TN", "48": "TX", "49": "UT",
    "50": "VT", "51": "VA", "53": "WA", "54": "WV", "55": "WI", "56": "WY",
}

SOURCES = {
    "model": ("SiteSense impact model (assumptions in backend/app/reference.py)", "https://github.com/sbaghba/geohack"),
    "egrid": ("EPA eGRID2023", "https://www.epa.gov/egrid"),
    "eia": ("EIA State Electricity Profiles 2024 (avg retail price)", "https://www.eia.gov/electricity/state/"),
    "ncdor": ("NC Dept. of Revenue county tax rates 2025-26", "https://www.ncdor.gov/taxes-forms/property-tax/property-tax-rates/county-property-tax-rates-and-reappraisal-schedules/fiscal-year-2025-2026"),
    "acs": ("US Census ACS 5-year 2023", "https://www.census.gov/data/developers.html"),
    "nri": ("FEMA National Risk Index (census tracts)", "https://hazards.fema.gov/nri/"),
    "nfhl": ("FEMA National Flood Hazard Layer", "https://www.fema.gov/flood-maps/national-flood-hazard-layer"),
    "aqueduct": ("WRI Aqueduct 4.0 baseline water stress", "https://www.wri.org/aqueduct"),
    "worldcover": ("ESA WorldCover 2021 (10 m)", "https://esa-worldcover.org"),
    "osm": ("OpenStreetMap contributors", "https://www.openstreetmap.org/copyright"),
    "census_tiger": ("US Census cartographic boundaries 2023", "https://www.census.gov/geographies/mapping-files/time-series/geo/cartographic-boundary.html"),
    "nisar": ("NASA-ISRO NISAR L2 (ASF DAAC)", "https://nisar-docs.asf.alaska.edu/"),
    "pressure_model": ("SiteSense Siting Pressure model (GPU XGBoost on OSM data centers, states held out)", "https://github.com/sbaghba/geohack"),
    "usdm": ("U.S. Drought Monitor (current week, via FEMA GIS)", "https://droughtmonitor.unl.edu/"),
    "opera": ("OPERA DISP-S1 (ASF DAAC)", "https://www.earthdata.nasa.gov/data/catalog/asf-opera-l3-disp-s1-v1-1"),
    "padus": ("USGS Protected Areas Database of the U.S. (PAD-US 4, GAP 1-3)", "https://www.usgs.gov/programs/gap-analysis-project/science/pad-us-data-overview"),
}


# Data-center campuses that OpenStreetMap maps without a name (osm_id -> widely reported operator/site).
DATACENTER_NAMES = {
    "w116005354": "Apple data center (Maiden)", "w652034566": "Apple data center (Maiden)", "w873137563": "Apple data center (Maiden)",
    "w186515922": "Google data center (Lenoir)", "w844372538": "Google data center (Lenoir)",
}
DATACENTER_CAMPUS_KM = 1.0   # OSM often maps each building; points closer than this are one campus
PROTECTED_SUIT_CAP = 10.0   # a site inside PAD-US protected land is not buildable: suitability capped, verdict poor/avoid
