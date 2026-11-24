"""GetMovely MCP server: the buyer's-search tools added in 0.4.0.

search_homes_for_sale, find_homes_not_for_sale and recent_sales read three
Flask routes written for them (the website's code: /api/homes/for-sale,
/api/homes/off-market, /api/sales/recent), which hold the SQL, the area parser
and the rate limits. stamp_duty is arithmetic on the published rates and needs
no data. draft_letter builds the link that opens the website's own letter
composer with the agreed text already in it; it sends nothing and stores
nothing, here or on the site.

Loaded by gm_mcp.py through register(core); see gm_mcp_homes.py for why this
module neither imports gm_mcp nor uses stringified annotations.
"""
import re
import time
from typing import Annotated, Literal
from urllib.parse import quote

import httpx
from pydantic import Field
from mcp.server.mcpserver import Context

AREA_DESC = ("Where to look: a postcode district ('BS7'), a full postcode ('SW19 1AB'), a town "
             "('Wimbledon'), or a street with its district or town ('Elm Road, BS7').")
PAGE_DESC = "Page number, 20 homes a page."
PER_PAGE = 20

# ---------------------------------------------------------------- stamp duty
# Rates and thresholds read from the primary sources on 2026-11-12 and checked again line by line on 2026-11-13. When either
# page changes, change the tables and RATES_READ together; test_mcp.py pins the
# worked examples the two pages themselves print.
#
# England (and Northern Ireland), Stamp Duty Land Tax:
#   https://www.gov.uk/stamp-duty-land-tax/residential-property-rates
#     "Up to £125,000 Zero; the next £125,000 (£125,001 to £250,000) 2%; the next
#      £675,000 (£250,001 to £925,000) 5%; the next £575,000 (£925,001 to £1.5
#      million) 10%; the remaining amount (above £1.5 million) 12%."
#     First-time buyers: "no SDLT up to £300,000; 5% SDLT on the portion from
#      £300,001 to £500,000. If the price is over £500,000, you cannot claim the
#      relief."
#     "You'll usually pay a 2% surcharge" when not a UK resident.
#   https://www.gov.uk/guidance/stamp-duty-land-tax-buying-an-additional-residential-property
#     "The higher rates from 1 April 2025: up to £125,000 5%; £125,001 to £250,000
#      7%; £250,001 to £925,000 10%; £925,001 to £1.5 million 15%; above £1.5
#      million 17%", for a purchase of "£40,000 or more".
# Wales, Land Transaction Tax:
#   https://www.gov.wales/land-transaction-tax-rates-and-bands
#     Main rates, effective date on or after 10 October 2022: "up to and including
#      £225,000 0%; over £225,000 up to £400,000 6%; over £400,000 up to £750,000
#      7.5%; over £750,000 up to £1,500,000 10%; over £1,500,000 12%."
#     Higher rates, on or after 11 December 2024: "up to and including £180,000
#      5%; to £250,000 8.5%; to £400,000 10%; to £750,000 12.5%; to £1,500,000
#      15%; over £1,500,000 17%."
#   https://www.gov.wales/land-transaction-tax-guide
#     "There's no first-time buyers' relief in Wales."
#   https://www.gov.wales/higher-rates-land-transaction-tax-overview
#     Higher rates when "you buy a residential property worth £40,000 or more"
#     and "you already own one or more other properties".
RATES_READ = "2026-11-13"
INF = float("inf")
SDLT_STANDARD = [(125_000, 0.0), (250_000, 2.0), (925_000, 5.0), (1_500_000, 10.0), (INF, 12.0)]
SDLT_FIRST_TIME = [(300_000, 0.0), (500_000, 5.0)]
SDLT_FIRST_TIME_MAX = 500_000
SDLT_ADDITIONAL = [(125_000, 5.0), (250_000, 7.0), (925_000, 10.0), (1_500_000, 15.0), (INF, 17.0)]
SDLT_NON_RESIDENT_POINTS = 2.0
LTT_MAIN = [(225_000, 0.0), (400_000, 6.0), (750_000, 7.5), (1_500_000, 10.0), (INF, 12.0)]
LTT_HIGHER = [(180_000, 5.0), (250_000, 8.5), (400_000, 10.0), (750_000, 12.5), (1_500_000, 15.0), (INF, 17.0)]
HIGHER_RATES_FROM = 40_000
SDLT_URL = "https://www.gov.uk/stamp-duty-land-tax/residential-property-rates"
SDLT_HIGHER_URL = "https://www.gov.uk/guidance/stamp-duty-land-tax-buying-an-additional-residential-property"
SDLT_CALC_URL = "https://www.tax.service.gov.uk/calculate-stamp-duty-land-tax/"
LTT_URL = "https://www.gov.wales/land-transaction-tax-rates-and-bands"
LTT_GUIDE_URL = "https://www.gov.wales/land-transaction-tax-guide"
LTT_CALC_URL = "https://www.gov.wales/land-transaction-tax-calculator"
LTT_POSTCODE_URL = "https://www.gov.wales/find-out-if-postcode-wales-land-transaction-tax"


def banded(price: int, bands: list, extra_points: float = 0.0) -> tuple[int, list]:
    """Tax on `price` over slice bands [(upper limit, percent)], with the
    slices that carried it. Whole pounds, rounded down per slice, as both
    official calculators do."""
    total, lower, rows = 0, 0, []
    for upper, pct in bands:
        if price <= lower:
            break
        part = min(price, upper) - lower
        rate = pct + extra_points
        # integer arithmetic: every published rate has at most one decimal
        tax = int(part) * int(round(rate * 10)) // 1000
        rows.append({"from_gbp": lower, "to_gbp": None if upper == INF else int(upper),
                     "rate_pct": rate, "taxed_gbp": int(part), "tax_gbp": tax})
        total += tax
        lower = upper
    return total, rows


def stamp_duty_calc(price: int, country: str, buyer: str, non_uk_resident: bool = False) -> dict:
    """The whole calculation, separate from the tool so the tests can pin it."""
    notes = []
    if country == "Wales":
        tax_name = "Land Transaction Tax (LTT)"
        if buyer == "additional" and price >= HIGHER_RATES_FROM:
            bands, basis = LTT_HIGHER, "higher residential rates (you already own another home)"
        else:
            bands, basis = LTT_MAIN, "main residential rates"
            if buyer == "first_time":
                notes.append("There is no first-time buyers' relief in Wales, so a first-time buyer "
                             "pays the main rates.")
            if buyer == "additional":
                notes.append("The higher rates apply from £40,000; below that the main rates apply.")
        if non_uk_resident:
            notes.append("Wales has no surcharge for buyers who are not UK residents.")
        tax, slices = banded(price, bands)
    else:
        tax_name = "Stamp Duty Land Tax (SDLT)"
        extra = SDLT_NON_RESIDENT_POINTS if non_uk_resident else 0.0
        if buyer == "first_time" and price <= SDLT_FIRST_TIME_MAX:
            bands, basis = SDLT_FIRST_TIME, "first-time buyer relief"
        elif buyer == "additional" and price >= HIGHER_RATES_FROM:
            bands, basis = SDLT_ADDITIONAL, "higher rates for an additional property"
        else:
            bands, basis = SDLT_STANDARD, "rates for a single property"
            if buyer == "first_time":
                notes.append("First-time buyer relief cannot be claimed above £500,000, so the "
                             "standard rates apply to the whole price.")
            if buyer == "additional":
                notes.append("The higher rates apply from £40,000; below that the standard rates apply.")
        if non_uk_resident:
            basis += ", plus the 2% surcharge for buyers who are not UK residents"
        tax, slices = banded(price, bands, extra)
    return {"tax_name": tax_name, "basis": basis, "tax_gbp": tax, "slices": slices, "notes": notes,
            "effective_rate_pct": round(100 * tax / price, 2) if price else 0.0}


def register(core):
    mcp, READ, AKey, logged = core.mcp, core.READ, core.AKey, core.logged
    _get, _ok, ToolFailure = core._get, core._ok, core.ToolFailure
    PUBLIC = core.PUBLIC
    # search_homes_for_sale speaks the advert's type words; the off-market
    # search speaks the certificate's, which has no detached/semi/terraced.
    OFF_MARKET_TYPE = {"house": "house", "detached": "house", "semi-detached": "house",
                       "terraced": "house", "flat": "flat", "bungalow": "bungalow"}

    def offset(page: int) -> int:
        return (page - 1) * PER_PAGE

    def home_bits(r: dict) -> dict:
        """What a matched home adds to a row: the same facts, label and links
        every other answer about a home carries."""
        label = core._label(r.get("addr") or r.get("title"), r.get("postcode"))
        state = core._state(r)
        return {"akey": r["akey"], "address": r.get("addr") or r.get("title"), "postcode": r.get("postcode"),
                "tenure_at_last_sale": r.get("tenure"), **core._home_facts(r), "state": state,
                "url": core._home_url(r["akey"], label),
                "contact_owner_url": core._contact_url(r["akey"], label, state)}

    def area_bits(data: dict) -> dict:
        a = data.get("area") or {}
        return {k: v for k, v in {
            "asked": a.get("asked"), "postcode_districts": a.get("postcode_districts"),
            "street": a.get("street"), "postcode": a.get("postcode"),
            "postcode_sector": a.get("postcode_sector")}.items() if v}

    # ------------------------------------------------------------ for sale
    @mcp.tool(title="Search homes for sale", annotations=READ)
    @logged("search_homes_for_sale")
    async def search_homes_for_sale(
        area: Annotated[str, Field(min_length=2, max_length=160, description=AREA_DESC)],
        max_price_gbp: Annotated[int | None, Field(ge=1, le=1_000_000_000, description="Highest asking price in pounds.")] = None,
        min_price_gbp: Annotated[int | None, Field(ge=0, le=1_000_000_000, description="Lowest asking price in pounds.")] = None,
        min_bedrooms: Annotated[int | None, Field(ge=0, le=20)] = None,
        max_bedrooms: Annotated[int | None, Field(ge=0, le=20)] = None,
        property_type: Annotated[Literal["house", "flat", "detached", "semi-detached", "terraced", "bungalow"] | None, Field(
            description="From the advert's own words. 'house' is any house including bungalows; "
                        "'flat' includes apartments and maisonettes; 'terraced' includes end of terrace.")] = None,
        min_floor_area_m2: Annotated[int | None, Field(ge=10, le=2000, description="Smallest floor area in square metres. Adverts with no floor area on record are left out.")] = None,
        tenure: Annotated[Literal["freehold", "leasehold"] | None, Field(
            description="As recorded at the home's last sale. Only adverts matched to an exact address can be filtered this way.")] = None,
        epc_ratings: Annotated[list[Literal["A", "B", "C", "D", "E", "F", "G"]] | None, Field(
            max_length=7, description="Energy ratings to keep, for example ['A','B','C']. Only adverts matched to an exact address.")] = None,
        owner_occupied: Annotated[bool | None, Field(
            description="True: only homes whose Energy Performance Certificate records them as owner-occupied "
                        "(response likelihood High). Only adverts matched to an exact address.")] = None,
        sort: Annotated[Literal["newest", "price_asc", "price_desc", "price_per_m2", "largest"], Field(
            description="'newest' advert first (default), by asking price, by asking price per square metre (lowest first), or largest first.")] = "newest",
        page: Annotated[int, Field(ge=1, le=50, description=PAGE_DESC)] = 1,
        ctx: Context = None,
    ) -> dict:
        """Homes currently advertised for sale in an area of England or Wales, by criteria:
        "find me a 3-bed under £400k in BS7", "flats over 60 m2 in Wimbledon". Filter by
        asking price, bedrooms, property type and floor area; and, for adverts GetMovely has
        matched to an exact address, by freehold or leasehold, energy rating and whether the
        owner lives there. Each row has the asking price, bedrooms, type, floor area, price
        per square metre, when it was listed and days on the market, the portal, agent and
        link to the advert, and for a matched home its address key, response likelihood and
        the link that opens a letter to its owner. 20 a page; `total` is every advert that
        fits. Adverts are re-read regularly, so a brand-new listing can be missing."""
        data = await _get("/api/homes/for-sale", {
            "area": area, "max_price": max_price_gbp, "min_price": min_price_gbp,
            "min_beds": min_bedrooms, "max_beds": max_bedrooms, "type": property_type,
            "min_sqm": min_floor_area_m2, "tenure": tenure,
            "epc": ",".join(epc_ratings) if epc_ratings else None,
            "owner_occupied": "1" if owner_occupied else None,
            "sort": sort, "limit": PER_PAGE, "offset": offset(page)}, ctx)
        rows, named = [], False
        for r in data.get("rows") or []:
            advert = core._advert(r)
            named = named or bool(advert)
            row = {"address": r.get("advert_address"),
                   "asking_price_gbp": r.get("asking_price"), "bedrooms": r.get("bedrooms"),
                   "bathrooms": r.get("bathrooms"), "property_type": r.get("property_type"),
                   "floor_area_m2": r.get("floor_m2"),
                   "asking_price_per_m2": r.get("price_per_m2"),
                   "listed_date": r.get("listed_date"), "days_on_market": r.get("days_on_market"),
                   "advert": advert, "matched_to_address": bool(r.get("akey"))}
            if r.get("akey"):
                row.update(home_bits(r))
                row.update({"energy_rating": r.get("epc_rating"),
                            "last_sale_price_gbp": r.get("last_sale"),
                            "last_sale_date": core._iso(r.get("last_date"))})
            else:
                row["akey"] = None
            rows.append(row)
        notes = ["Asking prices, bedrooms and types are the advert's own. Floor area is from the advert "
                 "or the home's Energy Performance Certificate.",
                 "A row with matched_to_address false is an advert GetMovely has not yet tied to an exact "
                 "address: read it at its advert link; find_address_from_sale_history can identify it "
                 "from the past sales the advert shows."]
        if data.get("address_filters"):
            notes.insert(0, "The tenure, energy rating, owner-occupied and full-postcode filters only reach "
                            "adverts matched to an exact address (about 4 in 10), so homes that fit can be "
                            "missing from this list. Run the search without them to see every advert.")
        if data.get("total") is None and tenure:
            notes.append("With a tenure filter the total is not counted; `more` says whether another page exists.")
        return _ok(area=area_bits(data), total=data.get("total"), matched_to_address=data.get("matched_to_address"),
                   page=page, per_page=PER_PAGE, more=bool(data.get("more")), sort=data.get("sort"),
                   rows=rows, notes=notes, about_these_fields=core.ABOUT_HOME_FACTS,
                   **({"portal_note": core.PORTAL_NOTE} if named else {}),
                   # the homes here that are NOT advertised, same area, ready to call
                   next_step=core._off_market_step(area, OFF_MARKET_TYPE.get(property_type)),
                   url=f"{PUBLIC}/search-listings",
                   sources=["Public property adverts read by GetMovely.", core.SRC_HMLR, core.SRC_EPC])

    # ------------------------------------------------------------ not for sale
    @mcp.tool(title="Find homes that are not for sale", annotations=READ)
    @logged("find_homes_not_for_sale")
    async def find_homes_not_for_sale(
        area: Annotated[str, Field(min_length=2, max_length=160, description=AREA_DESC + " A town of more than four postcode districts must be narrowed to a district.")],
        property_type: Annotated[Literal["house", "flat", "bungalow", "maisonette"] | None, Field(
            description="From the home's Energy Performance Certificate, which does not say detached, semi-detached or terraced.")] = None,
        min_floor_area_m2: Annotated[int | None, Field(ge=10, le=2000)] = None,
        max_floor_area_m2: Annotated[int | None, Field(ge=10, le=2000)] = None,
        tenure: Annotated[Literal["freehold", "leasehold"] | None, Field(
            description="As recorded at the home's last sale, so homes with no recorded sale are left out.")] = None,
        epc_ratings: Annotated[list[Literal["A", "B", "C", "D", "E", "F", "G"]] | None, Field(
            max_length=7, description="Energy ratings to keep, for example ['E','F','G'] for homes that need work.")] = None,
        last_sold_before_year: Annotated[int | None, Field(
            ge=1996, le=2100, description="Last sold before this year: long-held homes. Homes with no recorded "
                                         "sale since 1995 are included too unless no_recorded_sale says otherwise.")] = None,
        last_sold_after_year: Annotated[int | None, Field(ge=1995, le=2100, description="Last sold in or after this year.")] = None,
        no_recorded_sale: Annotated[Literal["include", "only", "exclude"] | None, Field(
            description="Homes with no sale on record since 1995 (held longest of all): 'only' those, or 'exclude' them.")] = None,
        min_last_sale_price_gbp: Annotated[int | None, Field(ge=0, le=1_000_000_000)] = None,
        max_last_sale_price_gbp: Annotated[int | None, Field(ge=0, le=1_000_000_000)] = None,
        owner_occupied: Annotated[bool | None, Field(
            description="True: only homes whose Energy Performance Certificate records them as owner-occupied "
                        "(response likelihood Medium), where a letter reaches the owner.")] = None,
        sort: Annotated[Literal["owner_occupied_first", "longest_held", "recent", "price_asc", "price_desc", "largest"], Field(
            description="'owner_occupied_first' (default, the website's order), 'longest_held' oldest last sale "
                        "first, 'recent' newest sale first, by last sale price, or largest floor area first.")] = "owner_occupied_first",
        page: Annotated[int, Field(ge=1, le=50, description=PAGE_DESC)] = 1,
        ctx: Context = None,
    ) -> dict:
        """Homes that are NOT advertised for sale, by criteria: the homes a buyer could write
        to. "Which houses on this street could I write to", "owner-occupied houses over
        120 m2 in BS7 that last sold before 2005". Filter by property type, floor area,
        freehold or leasehold, energy rating, when and for how much the home last sold, and
        whether the owner lives there. Each row has the address, type, floor area, energy
        rating, last sale and its price per square metre, whether it is owner-occupied, its
        response likelihood, and `contact_owner_url`, the link that opens a letter to that
        home's owner on GetMovely. 20 a page."""
        data = await _get("/api/homes/off-market", {
            "area": area, "type": property_type, "min_sqm": min_floor_area_m2, "max_sqm": max_floor_area_m2,
            "tenure": tenure, "epc": ",".join(epc_ratings) if epc_ratings else None,
            "sold_before": last_sold_before_year, "sold_after": last_sold_after_year,
            "never_sold": no_recorded_sale, "min_price": min_last_sale_price_gbp,
            "max_price": max_last_sale_price_gbp, "owner_occupied": "1" if owner_occupied else None,
            "sort": sort, "limit": PER_PAGE, "offset": offset(page)}, ctx)
        rows = []
        for r in data.get("rows") or []:
            row = home_bits(r)
            row.update({"property_type": r.get("property_type"), "floor_area_m2": r.get("floor_m2"),
                        "energy_rating": r.get("epc_rating"),
                        "last_sale_price_gbp": r.get("last_sale"),
                        "last_sale_date": core._iso(r.get("last_date")),
                        "last_sale_gbp_per_m2": r.get("last_sale_per_m2"),
                        "sales_on_record": r.get("n_sales")})
            rows.append(row)
        notes = ["Not for sale means GetMovely has not seen a current public advert for the home; adverts "
                 "are re-read regularly, so a brand-new listing can be missing. get_address confirms one home.",
                 "Type, floor area and energy rating come from the home's latest Energy Performance "
                 "Certificate; a home without one cannot match those filters.",
                 "Writing to an owner is lawful and has rules: read_guide "
                 "'is-it-legal-to-write-to-a-homeowner' before drafting, then draft_letter."]
        if data.get("total") is None and tenure:
            notes.append("With a tenure filter the total is not counted; `more` says whether another page exists.")
        a = data.get("area") or {}
        q_text = " ".join(x for x in [a.get("street"), a.get("postcode") or a.get("postcode_sector")
                                     or (a.get("postcode_districts") or [""])[0]] if x) or area
        return _ok(area=area_bits(data), total=data.get("total"), page=page, per_page=PER_PAGE,
                   more=bool(data.get("more")), sort=data.get("sort"), rows=rows, notes=notes,
                   about_these_fields=core.ABOUT_HOME_FACTS,
                   # the same search on the website's off-market finder, which
                   # ticks "owner-occupied" unless told otherwise
                   url=(f"{PUBLIC}/tools/homes-not-for-sale?area={core._q(q_text)}"
                        + (f"&type={property_type}" if property_type else "")
                        + (f"&min_sqm={min_floor_area_m2}" if min_floor_area_m2 else "")
                        + ("" if owner_occupied else "&owner_occupied=0")),
                   sources=[core.SRC_HMLR, core.SRC_EPC, core.SRC_OS])

    # ------------------------------------------------------------ sold prices
    @mcp.tool(title="Recent sold prices in an area", annotations=READ)
    @logged("recent_sales")
    async def recent_sales(
        area: Annotated[str, Field(min_length=2, max_length=160, description=AREA_DESC + " A town of more than six postcode districts must be narrowed to a district.")],
        property_type: Annotated[Literal["D", "S", "T", "F"] | None, Field(
            description="D detached, S semi-detached, T terraced, F flat (the Land Registry's own classes).")] = None,
        sold_from: Annotated[str | None, Field(
            max_length=10, pattern=r"^\d{4}(-\d{2}-\d{2})?$",
            description="Earliest sale date, 'YYYY-MM-DD', or a year ('2026' means from 1 January 2026).")] = None,
        sold_to: Annotated[str | None, Field(
            max_length=10, pattern=r"^\d{4}(-\d{2}-\d{2})?$",
            description="Latest sale date, 'YYYY-MM-DD', or a year ('2025' means to 31 December 2025).")] = None,
        min_price_gbp: Annotated[int | None, Field(ge=0, le=1_000_000_000)] = None,
        max_price_gbp: Annotated[int | None, Field(ge=0, le=1_000_000_000)] = None,
        min_floor_area_m2: Annotated[int | None, Field(
            ge=10, le=2000, description="Only sales of homes at least this big. Needs a floor area on an Energy "
                                        "Performance Certificate; with this filter the total is not counted.")] = None,
        tenure: Annotated[Literal["freehold", "leasehold"] | None, Field(description="As recorded on the sale.")] = None,
        new_build: Annotated[Literal["only", "exclude"] | None, Field(description="New-build sales only, or leave them out.")] = None,
        page: Annotated[int, Field(ge=1, le=50, description="Page number, 20 sales a page.")] = 1,
        ctx: Context = None,
    ) -> dict:
        """What sold in an area, newest first: "what sold near here this year", "flats sold in
        SW19 since January for under £500k". HM Land Registry price paid data for a postcode
        district, postcode, street or small town, filtered by property type, date range,
        price range, floor area, tenure and new build. Each sale has the address, price,
        date, type, freehold or leasehold, and the price per square metre where the home has
        a floor area on its Energy Performance Certificate. Market-value sales only. Sales
        reach the register two weeks to two months after completion, so the last few weeks
        are always incomplete. For one home's own history use sale_history."""
        data = await _get("/api/sales/recent", {
            "area": area, "type": property_type, "from": sold_from, "to": sold_to,
            "min_price": min_price_gbp, "max_price": max_price_gbp, "min_sqm": min_floor_area_m2,
            "tenure": tenure, "new_build": new_build, "limit": PER_PAGE, "offset": offset(page)}, ctx)
        rows = []
        for r in data.get("rows") or []:
            label = core._label(r.get("addr"), r.get("postcode"))
            rows.append({"address": r.get("addr"), "postcode": r.get("postcode"),
                         "price_gbp": r.get("price"), "sold": r.get("sold"),
                         "property_type": r.get("property_type"), "new_build": r.get("new_build"),
                         "tenure": r.get("tenure"), "floor_area_m2": r.get("floor_m2"),
                         "gbp_per_m2": r.get("price_per_m2"), "akey": r.get("akey"),
                         "url": core._home_url(r["akey"], label) if r.get("akey") else None})
        sized = [r["gbp_per_m2"] for r in rows if r["gbp_per_m2"]]
        notes = ["Market-value (category A) sales only; repossessions, bulk and company transfers are left out.",
                 "Price per square metre divides the sale price by the floor area on the home's current "
                 "Energy Performance Certificate, so a home extended since reads high and one without a "
                 "certificate has none.",
                 "Sales reach the register two weeks to two months after completion."]
        if data.get("total") is None and min_floor_area_m2:
            notes.append("With a floor area filter the total is not counted; `more` says whether another page exists.")
        a = data.get("area") or {}
        ocs = a.get("postcode_districts") or []
        url = f"{PUBLIC}/?q={core._q(area)}"
        if len(ocs) == 1 and ocs[0] in await core._area_outcodes():
            url = f"{PUBLIC}/sold-prices/{ocs[0].lower()}" + (f"/{core._slug(a['street'])}" if a.get("street") else "")
        return _ok(area=area_bits(data), total=data.get("total"), page=page, per_page=PER_PAGE,
                   more=bool(data.get("more")), rows=rows,
                   this_page={"sales": len(rows), "with_floor_area": len(sized),
                              "median_gbp_per_m2": sorted(sized)[len(sized) // 2] if sized else None},
                   notes=notes, url=url, sources=[core.SRC_HMLR, core.SRC_EPC])

    # ------------------------------------------------------------ stamp duty
    @mcp.tool(title="Stamp duty calculator (SDLT in England, LTT in Wales)", annotations=READ)
    @logged("stamp_duty")
    async def stamp_duty(
        price_gbp: Annotated[int, Field(ge=1, le=1_000_000_000, description="Purchase price in pounds.")],
        country: Annotated[Literal["England", "Wales"], Field(
            description="Where the home is. England uses Stamp Duty Land Tax; Wales uses Land Transaction Tax.")] = "England",
        buyer: Annotated[Literal["home_mover", "first_time", "additional"], Field(
            description="'home_mover': replacing a main home, the standard rates. 'first_time': every buyer is "
                        "a first-time buyer. 'additional': the buyer will own more than one home after the "
                        "purchase (second home, buy to let).")] = "home_mover",
        non_uk_resident: Annotated[bool, Field(description="Buyer is not a UK resident (adds 2% in England).")] = False,
        ctx: Context = None,
    ) -> dict:
        """How much stamp duty: Stamp Duty Land Tax for a home in England, Land Transaction Tax
        for one in Wales, for a home mover, a first-time buyer or an additional property,
        with the tax in each band and the other two buyer cases beside it. Uses the
        residential rates published on GOV.UK and gov.wales, quoted in `sources` with the
        date they were read. An estimate for a standard purchase of one freehold or
        existing leasehold home by individuals: shared ownership, new leases with rent,
        companies, mixed-use property and linked purchases follow other rules, and the
        official calculator is linked in the answer."""
        got = stamp_duty_calc(price_gbp, country, buyer, non_uk_resident)
        others = {b: stamp_duty_calc(price_gbp, country, b, non_uk_resident)["tax_gbp"]
                  for b in ("home_mover", "first_time", "additional")}
        wales = country == "Wales"
        caveats = [
            "An estimate from the published residential rates, not tax advice. The buyer's solicitor or "
            "conveyancer files the return and confirms the amount.",
            "Assumes one home bought by individuals as a freehold or an existing lease. Shared ownership, "
            "a new lease with rent, companies, mixed-use property, several dwellings in one purchase and "
            "linked purchases follow other rules.",
            ("Higher rates apply when the buyer, or anyone they are buying with or married to, will own "
             "more than one home after the purchase; replacing a main home can be exempt or refunded."),
        ]
        if wales:
            caveats.append("A home near the border: check which tax applies by postcode at " + LTT_POSTCODE_URL)
        return _ok(
            price_gbp=price_gbp, country=country, buyer=buyer, non_uk_resident=non_uk_resident,
            tax=got["tax_name"], basis=got["basis"], tax_gbp=got["tax_gbp"],
            effective_rate_pct=got["effective_rate_pct"], bands=got["slices"],
            same_price_other_buyers_gbp=others, notes=got["notes"], caveats=caveats,
            rates_read_on=RATES_READ,
            official_calculator_url=LTT_CALC_URL if wales else SDLT_CALC_URL,
            url=LTT_URL if wales else SDLT_URL,
            sources=([f"Welsh Government, Land Transaction Tax rates and bands, {LTT_URL} (read {RATES_READ}); "
                      f"no first-time buyers' relief in Wales: {LTT_GUIDE_URL}. Open Government Licence v3.0."]
                     if wales else
                     [f"GOV.UK, Stamp Duty Land Tax: residential property rates, {SDLT_URL}, and higher rates "
                      f"for additional properties, {SDLT_HIGHER_URL} (read {RATES_READ}). "
                      "Contains public sector information licensed under the Open Government Licence v3.0."]))

    # ------------------------------------------------------------ the letter hand-off
    LIMITS_TTL = 3600
    limits = {"at": 0.0, "max_words": 200, "max_chars": 1400}

    async def letter_limits() -> dict:
        """The website's own limits for a letter (/api/meta, where send_letter's
        constants are published), cached an hour; the last good values on a
        failed read."""
        if time.time() - limits["at"] > LIMITS_TTL:
            try:
                r = await core._http().get("/api/meta", timeout=5, headers={
                    "User-Agent": f"GetMovely-MCP/{core.VERSION} meta", "X-GM-Probe": "mcp-meta"})
                got = r.json().get("letter_limits") or {}
                limits.update(max_words=int(got.get("max_words") or limits["max_words"]),
                              max_chars=int(got.get("max_chars") or limits["max_chars"]), at=time.time())
            except (httpx.HTTPError, ValueError, TypeError):
                pass
        return limits

    CONTACT_RE = re.compile(r"[\w.+-]+@[\w-]+\.[\w.]+|(?:\+44|\b0)\d[\d ]{8,12}\d")

    @mcp.tool(title="Open a drafted letter to a home's owner on GetMovely", annotations=READ)
    @logged("draft_letter")
    async def draft_letter(
        akey: AKey,
        letter_text: Annotated[str, Field(
            min_length=1, max_length=6000,
            description="The message the person has agreed, as plain text: what goes in the body of the letter. "
                        "No address block, date or sign-off name is needed; GetMovely lays the letter out.")],
        ctx: Context = None,
    ) -> dict:
        """Hand a letter the person has agreed with you to GetMovely. Returns `letter_url`: a
        link that opens the letter composer for that home on getmovely.co.uk with the text
        already filled in, where the person reads it, can edit it, and sends it themselves
        (free; sending needs a free account, and GetMovely prints and posts it). Nothing is
        sent and nothing is stored by this call: the text travels inside the link. Checks
        the website's letter rules first (200 words, 1,400 characters) and says what to fix
        if the text breaks them. The text is the person's own; the guide
        'letter-to-homeowner-asking-to-buy-house' says what a good letter contains."""
        lim = await letter_limits()
        text = re.sub(r"[ \t]+\n", "\n", letter_text.replace("\r\n", "\n").replace("\r", "\n")).strip()
        text = re.sub(r"\n{3,}", "\n\n", text)
        text = "".join(ch for ch in text if ch == "\n" or ch >= " ")
        words, chars = len(text.split()), len(text)
        problems = []
        if not text:
            problems.append("The letter is empty.")
        if words > lim["max_words"]:
            problems.append(f"The letter is {words} words; GetMovely letters are {lim['max_words']} words "
                            f"or fewer so they fit on one page. Cut {words - lim['max_words']} words.")
        if chars > lim["max_chars"]:
            problems.append(f"The letter is {chars:,} characters; the limit is {lim['max_chars']:,}.")
        data = await _get("/api/address", {"akey": akey}, ctx)
        r = data["row"]
        label = core._label(r.get("addr") or r.get("title"), r.get("postcode"))
        state = core._state(r)
        base = dict(akey=akey, address=r.get("addr") or r.get("title"), postcode=r.get("postcode"),
                    words=words, characters=chars,
                    limits={"max_words": lim["max_words"], "max_characters": lim["max_chars"]},
                    **core._home_facts(r), url=core._home_url(akey, label),
                    sources=["GetMovely letter rules, as enforced when a letter is sent."])
        if state == "owner_on_platform":
            return _ok(**base, ready=False, letter_url=None,
                       problems=["This home's owner is already on GetMovely, so letters to it are not "
                                 "posted; the person can message the owner directly instead."],
                       contact_owner_url=core._contact_url(akey, label, state))
        if problems:
            return _ok(**base, ready=False, letter_url=None, problems=problems,
                       contact_owner_url=core._contact_url(akey, label, state))
        advice = []
        if CONTACT_RE.search(text):
            advice.append("The text seems to include an e-mail address or phone number. That is the person's "
                          "choice, but it is not needed: the owner replies through GetMovely and the person "
                          "decides later whether to share contact details.")
        if r.get("tier") not in (2, 3):
            advice.append("Public records do not show that the owner lives at this home, so the letter may "
                          "reach a tenant; it can ask to be passed to the owner.")
        # The text rides in the URL FRAGMENT: a browser never sends a fragment to
        # a server, so the draft reaches the composer without touching
        # GetMovely's logs, Cloudflare's, or any stored state. app.js reads
        # #draft= on /?letter=<akey>, puts it in the textarea as text (escaped),
        # trims it to the same limits and clears the fragment.
        link = f"{PUBLIC}/?letter={akey}#draft={quote(text, safe='')}"
        return _ok(**base, ready=True, letter_url=link, problems=[], advice=advice,
                   next_step="Give the person letter_url. It opens the letter to this home with the text "
                             "filled in; they review it, preview it and send it themselves. Sending needs "
                             "a free GetMovely account and is limited to two letters a week.",
                   nothing_sent="This call sent nothing and stored nothing.")
