"""GetMovely MCP server: the area tools added in 0.2.0.

area_overview and compare_areas read the price-trend series the website's
/tools/price-trends page shows, one postcode district and property type at a
time, and lay them side by side. district_statistics reads the published
research CSVs (leasehold share, how long people stay, two-year resales, home
sizes) and returns one local authority district's rows instead of a whole
table. Nothing here is computed from anything the website does not publish.

Loaded by gm_mcp.py through register(core); see gm_mcp_homes.py for why this
module neither imports gm_mcp nor uses stringified annotations.
"""
import csv
import difflib
import io
import re
import time
from typing import Annotated, Literal

import httpx
from pydantic import Field
from mcp.server.mcpserver import Context

PT_LABEL = {"D": "detached houses", "S": "semi-detached houses", "T": "terraced houses", "F": "flats"}
TYPES = ("D", "S", "T", "F")
OUTCODE_RE = re.compile(r"[A-Z]{1,2}\d[A-Z\d]?")
METHOD = ("Medians of HM Land Registry sales matched to an Energy Performance Certificate floor area, "
          "whole calendar years. The year in progress is set against the same months a year earlier, "
          "because sales reach the register weeks to months late. A median can move because "
          "different homes sold, not only because prices did. The England and Wales figure is the "
          "median over the whole series for the same property type.")

CSV_TTL = 6 * 3600
STUDY_PAGES = {
    "leasehold": "/research/leasehold-freehold-statistics",
    "holding": "/research/how-long-people-stay-in-their-homes",
    "size": "/research/how-big-are-homes-in-england-and-wales",
}
CSVS = {
    "leasehold": "leasehold-freehold-statistics.csv",
    "holding": "how-long-people-stay-in-their-homes.csv",
    "flips": "how-long-people-stay-in-their-homes-flips.csv",
    "size": "how-big-are-homes-in-england-and-wales.csv",
    "built_form": "how-big-are-homes-in-england-and-wales-built-form.csv",
}
NATIONAL = "ENGLAND AND WALES"
LONDON = "GREATER LONDON (ALL DISTRICTS)"
ALIASES = {"LONDON": LONDON, "GREATER LONDON": LONDON, "ENGLAND": NATIONAL, "WALES": NATIONAL,
           "ENGLAND AND WALES": NATIONAL, "NATIONAL": NATIONAL, "UK": NATIONAL, "ALL": NATIONAL}
_STOP = {"CITY", "OF", "THE", "COUNTY", "BOROUGH", "ROYAL", "DISTRICT", "COUNCIL"}


def _key(name: str) -> str:
    """'City of Bristol', 'BRISTOL' and 'Bristol, City of' all become 'BRISTOL'."""
    return " ".join(t for t in re.sub(r"[^A-Z ]", " ", (name or "").upper()).split() if t not in _STOP)


def _title(name: str) -> str:
    """'CITY OF KINGSTON UPON HULL' -> 'City of Kingston upon Hull'."""
    words = (name or "").title().split()
    return " ".join(w.lower() if i and w.lower() in ("of", "upon", "and", "the", "on", "under", "in", "le")
                    else w for i, w in enumerate(words))


def _num(v):
    if v is None or v == "":
        return None
    try:
        f = float(v)
    except (TypeError, ValueError):
        return v
    return int(f) if f.is_integer() else f


def register(core):
    mcp, READ, logged = core.mcp, core.READ, core.logged
    _get, _ok, _gather, ToolFailure = core._get, core._ok, core._gather, core.ToolFailure
    PUBLIC = core.PUBLIC

    def outcode_of(text: str) -> str:
        oc = (text or "").strip().upper().split(" ")[0]
        if not OUTCODE_RE.fullmatch(oc):
            raise ToolFailure(f"{text!r} is not a postcode district. Give the first half of a postcode, "
                              "such as 'SW19', 'BS7' or 'M6'.")
        return oc

    def type_row(pt: str, tr: dict, floor: int | None = None) -> dict | None:
        series = tr.get("baseline") or []
        if tr.get("ok") is False or not series:
            return None
        first, latest = series[0], series[-1]
        row = {"property_type": PT_LABEL[pt], "code": pt, "year": latest["yr"],
               "median_gbp_per_m2": latest["med"], "lower_quartile_gbp_per_m2": latest["q1"],
               "upper_quartile_gbp_per_m2": latest["q3"], "sales": latest.get("n"),
               "since_year": first["yr"], "median_then_gbp_per_m2": first["med"],
               "change_pct": round(100 * (latest["med"] / first["med"] - 1), 1) if first.get("med") else None,
               "england_and_wales_median_gbp_per_m2_all_years": tr.get("national_baseline_pooled")}
        ytd, part = tr.get("baseline_partial"), tr.get("partial")
        if ytd and part:
            row["year_to_date"] = {"year": ytd.get("yr"), "months": part.get("months"),
                                   "median_gbp_per_m2": ytd.get("med"), "sales": ytd.get("n"),
                                   "change_vs_same_months_a_year_earlier_pct": ytd.get("pct")}
        if floor:
            row["price_at_median_gbp"] = int(round(latest["med"] * floor / 1000.0)) * 1000
        return row

    # ------------------------------------------------------------ one district
    @mcp.tool(title="Overview of one postcode district", annotations=READ)
    @logged("area_overview")
    async def area_overview(
        outcode: Annotated[str, Field(min_length=2, max_length=8,
                                      description="Postcode district such as 'SW19', 'BS7', 'M6'. A full "
                                                  "postcode is accepted; only the district is used.")],
        ctx: Context = None,
    ) -> dict:
        """What homes sell for in one postcode district, for every property type sold
        there (detached, semi-detached, terraced, flats): the latest whole year's median
        price per square metre with its quartiles and number of sales, the change since
        the series began in 2015, and the year in progress against the same months a year
        earlier. Use compare_areas to set districts against each other and price_trends
        for what tenure, energy rating or size are worth within one."""
        oc = outcode_of(outcode)
        got = await _gather([_get("/api/price-trends", {"oc": oc, "pt": pt, "factor": "all"}, ctx, soft=(404,))
                             for pt in TYPES])
        rows = []
        for pt, tr in zip(TYPES, got):
            if isinstance(tr, Exception):
                raise tr
            row = type_row(pt, tr)
            if row:
                rows.append(row)
        if not rows:
            raise ToolFailure(f"No sales with a floor area on record for {oc}. Check the district; only "
                              "England and Wales are covered.")
        most = max(rows, key=lambda r: r.get("sales") or 0)
        out = dict(outcode=oc, property_types=rows, most_sold_type=most["property_type"], method=METHOD,
                   url=core._trends_url(oc, most["code"]), sources=[core.SRC_HMLR, core.SRC_EPC])
        if oc in await core._area_outcodes():
            out["sold_prices_by_street"] = f"{PUBLIC}/sold-prices/{oc.lower()}"
        return _ok(**out)

    # ------------------------------------------------------------ several districts
    @mcp.tool(title="Compare postcode districts", annotations=READ)
    @logged("compare_areas")
    async def compare_areas(
        outcodes: Annotated[list[str], Field(min_length=2, max_length=6,
                                             description="Two to six postcode districts, for example "
                                                         "['SW19', 'BS7', 'M20'].")],
        property_type: Annotated[Literal["D", "S", "T", "F"], Field(
            description="One type, held constant across the districts: D detached, S semi-detached, "
                        "T terraced, F flat.")] = "T",
        floor_area_m2: Annotated[int | None, Field(
            ge=15, le=600, description="Optional floor area to price at each district's median, "
                                       "for example 100.")] = None,
        ctx: Context = None,
    ) -> dict:
        """Two to six postcode districts side by side for one property type: latest whole
        year's median price per square metre with quartiles and sales, change since 2015,
        the year in progress against the same months a year earlier and, when a floor area
        is given, what that many square metres cost at each district's median. Property
        type is held constant because comparing a district of flats with a district of
        houses compares the mix, not the place."""
        ocs = []
        for o in outcodes:
            oc = outcode_of(o)
            if oc not in ocs:
                ocs.append(oc)
        if len(ocs) < 2:
            raise ToolFailure("Give at least two different postcode districts.")
        got = await _gather([_get("/api/price-trends", {"oc": oc, "pt": property_type, "factor": "all"},
                                  ctx, soft=(404,)) for oc in ocs], limit=3)
        rows = []
        for oc, tr in zip(ocs, got):
            if isinstance(tr, Exception):
                raise tr
            row = type_row(property_type, tr, floor_area_m2)
            if row:
                row.pop("property_type"), row.pop("code")
                rows.append({"outcode": oc, **row, "url": core._trends_url(oc, property_type)})
            else:
                here = [PT_LABEL[t] for t in tr.get("types_here") or [] if t in PT_LABEL]
                rows.append({"outcode": oc, "no_data": True,
                             "reason": f"too few sales of {PT_LABEL[property_type]} with a floor area in {oc}",
                             "types_with_data": here})
        priced = [r for r in rows if not r.get("no_data")]
        if not priced:
            raise ToolFailure(f"None of those districts has enough sales of {PT_LABEL[property_type]} "
                              "with a floor area to compare.")
        return _ok(property_type=PT_LABEL[property_type], floor_area_m2=floor_area_m2, districts=rows,
                   cheapest_first=[r["outcode"] for r in sorted(priced, key=lambda r: r["median_gbp_per_m2"])],
                   method=METHOD, url=core._trends_url(priced[0]["outcode"], property_type),
                   sources=[core.SRC_HMLR, core.SRC_EPC])

    # ------------------------------------------------------------ research by district
    cache: dict[str, tuple[float, list[dict]]] = {}

    async def table(which: str, ctx) -> list[dict]:
        at, rows = cache.get(which, (0.0, []))
        if rows and time.time() - at < CSV_TTL:
            return rows
        try:
            text = await core._get_text(f"/research/{CSVS[which]}", ctx)
        except httpx.HTTPError as e:
            core._note(f"backend {type(e).__name__}")
            raise ToolFailure("GetMovely is not answering right now; try again in a minute.") from e
        rows = list(csv.DictReader(io.StringIO(text)))
        cache[which] = (time.time(), rows)
        return rows

    def pick(rows: list[dict], names: set[str], fields: dict[str, str]) -> list[dict]:
        """Rows for the matched district names, with the published columns renamed
        to say what they are and their numbers as numbers."""
        return [{new: _num(r.get(old)) for old, new in fields.items()}
                for r in rows if r.get("district") in names]

    LEASE = {"district": "district", "properties": "homes", "leasehold_pct": "leasehold_pct",
             "houses": "houses", "leasehold_houses_pct": "houses_leasehold_pct",
             "flats": "flats", "leasehold_flats_pct": "flats_leasehold_pct"}
    HOLD = {"property_type": "property_type", "median_years": "median_years_between_sales",
            "lower_quartile_years": "lower_quartile_years", "upper_quartile_years": "upper_quartile_years",
            "long_held_pct": "not_sold_again_for_20_years_or_more_pct", "homes_sold_since_1995": "homes_sold_since_1995"}
    FLIP = {"property_type": "property_type", "purchase_years": "bought_in", "homes_bought": "homes_bought",
            "sold_again_within_2y_pct": "sold_again_within_two_years_pct"}
    SIZE = {"property_type": "property_type", "n": "homes", "median_m2": "median_m2", "p25_m2": "lower_quartile_m2",
            "p75_m2": "upper_quartile_m2", "share_under_50": "under_50_m2_pct", "share_over_150": "over_150_m2_pct"}
    FORM = dict(SIZE, built_form="built_form")
    FORM.pop("property_type")

    def sections(t: dict[str, list[dict]], names: set[str]) -> dict:
        return {"leasehold": pick(t["leasehold"], names, LEASE),
                "how_long_people_stay": pick(t["holding"], names, HOLD),
                "resold_within_two_years": pick(t["flips"], names, FLIP),
                "home_size": pick(t["size"], names, SIZE),
                "home_size_by_built_form": pick(t["built_form"], names, FORM)}

    @mcp.tool(title="Research figures for a local authority district", annotations=READ)
    @logged("district_statistics")
    async def district_statistics(
        district: Annotated[str, Field(
            min_length=2, max_length=80,
            description="A local authority district in England or Wales by name: 'Bristol', 'Camden', "
                        "'Merton', 'Leeds', 'Cardiff'. 'London' gives Greater London as a whole and "
                        "'England and Wales' the national figures.")],
        ctx: Context = None,
    ) -> dict:
        """GetMovely's published research for one local authority district, with England
        and Wales alongside: the share of homes that are leasehold (houses and flats
        separately), how many years pass between sales of the same home and the share not
        sold for 20 years or more, the share of purchases resold within two years, and how
        big homes are (median floor area, by property type and built form). Districts are
        named as HM Land Registry and the EPC register name them; an unclear name returns
        the candidates. For prices use area_overview, which works by postcode district."""
        got = await _gather([table(k, ctx) for k in CSVS], limit=3)
        for g in got:
            if isinstance(g, Exception):
                raise g
        t = dict(zip(CSVS, got))
        names = sorted({r.get("district") for rows in t.values() for r in rows if r.get("district")})
        want = ALIASES.get(re.sub(r"\s+", " ", district.strip().upper()))
        if want:
            matched = {want}
        else:
            qk = _key(district)
            matched = {n for n in names if _key(n) == qk} if qk else set()
            if not matched and qk:
                sub = sorted(n for n in names if set(qk.split()) <= set(_key(n).split()))
                keys = {_key(n) for n in sub}
                if len(keys) == 1:
                    matched = set(sub)
                elif not sub:
                    by_key = {_key(n): n for n in names}
                    sub = [by_key[k] for k in difflib.get_close_matches(qk, list(by_key), n=6, cutoff=0.72)]
                if not matched:
                    if not sub:
                        raise ToolFailure(f"No district called {district!r} in the research tables. Give the "
                                          "local authority's name, such as 'Bristol', 'Camden' or 'Leeds'; "
                                          "for a postcode district use area_overview.")
                    return _ok(district=None, query=district,
                               candidates=[_title(n) for n in sub[:20]],
                               hint=("Several districts match. Ask which, then call again with that name."
                                     if len(sub) > 1 else
                                     "No district has exactly that name; the closest is listed. Call again "
                                     "with it if that is the one meant."),
                               url=f"{PUBLIC}{STUDY_PAGES['holding']}", sources=[core.SRC_HMLR, core.SRC_EPC])
        out = sections(t, matched)
        return _ok(
            district=_title(max(matched, key=len)), names_in_the_tables=sorted(matched), **out,
            england_and_wales=(None if matched == {NATIONAL} else
                               {k: v for k, v in sections(t, {NATIONAL}).items() if v}),
            notes=["Leasehold: properties with a standard recorded sale since 1995, by tenure at their most "
                   "recent sale. It describes homes that have sold, not the whole housing stock, and keeps "
                   "HM Land Registry's own district names, so a renamed district can appear under each name.",
                   "Years between sales: the median gap between consecutive sales of the same home, a sample "
                   "of completed moves only. Homes sold once and never again are in the 'not sold again for "
                   "20 years or more' share, which is of homes sold since 1995.",
                   "Home size: the total floor area on each home's latest Energy Performance Certificate, so "
                   "homes without a certificate are not counted.",
                   "An empty section means the district is absent from that study's table."],
            study_pages={k: f"{PUBLIC}{v}" for k, v in STUDY_PAGES.items()},
            url=f"{PUBLIC}{STUDY_PAGES['holding']}", sources=[core.SRC_HMLR, core.SRC_EPC])
