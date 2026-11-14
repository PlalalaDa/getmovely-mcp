"""GetMovely MCP server: the per-home tools added in 0.2.0.

sale_history, energy_performance, price_check, compare_homes and
find_address_from_sale_history. Each composes existing public Flask routes
(the address report, the address card, the price-trend series, the address
search); none reads the database or adds a fact the website does not show.

Loaded by gm_mcp.py through register(core). This module never imports gm_mcp:
run as a script that file runs as __main__, and importing it again under its own
name would build a second server object with half the tools on each.

No `from __future__ import annotations` here on purpose: the tool signatures
use names that exist only inside register() (AKey), which Python 3.14 resolves
lazily through the closure; stringified annotations would look them up in the
module globals and fail.
"""
from typing import Annotated, Literal

from pydantic import Field
from mcp.server.mcpserver import Context

PT_LABEL = {"D": "detached houses", "S": "semi-detached houses", "T": "terraced houses", "F": "flats"}

CAVEATS = [
    "Arithmetic on public sale records, not a valuation.",
    "Floor area comes from the Energy Performance Certificate: a home extended since its certificate reads small, which makes its price per square metre look high.",
    "A district median moves with the mix of homes sold as well as with prices.",
    "Condition, lease length, floor level, outside space and exact position are not in the data.",
]


def _round_k(v: float) -> int:
    """To the nearest thousand pounds: an implied price to the pound would claim
    a precision a median times a floor area does not have."""
    return int(round(v / 1000.0)) * 1000


def register(core):
    mcp, READ, AKey, logged = core.mcp, core.READ, core.AKey, core.logged
    _get, _ok, _gather, ToolFailure = core._get, core._ok, core._gather, core.ToolFailure
    _tiles, _home_url, _report_label, _report_address = (core._tiles, core._home_url,
                                                        core._report_label, core._report_address)
    _home_facts, _state, _contact_url = core._home_facts, core._state, core._contact_url
    PUBLIC = core.PUBLIC

    def detail(tiles: dict, key: str) -> dict:
        return (tiles.get(key) or {}).get("detail") or {}

    def verdict(tiles: dict, key: str):
        return (tiles.get(key) or {}).get("verdict")

    # ------------------------------------------------------------ sale history
    @mcp.tool(title="Sale history of one home", annotations=READ)
    @logged("sale_history")
    async def sale_history(akey: AKey, ctx: Context = None) -> dict:
        """Every recorded sale of one home since 1995, from HM Land Registry price paid
        data: price, month and year, the price per square metre each sale works out at on
        the home's current EPC floor area, and the change from the first sale to the last.
        The short answer to "what did this house sell for"; address_report has the rest."""
        rep = await _get("/api/address-report", {"akey": akey}, ctx)
        t = _tiles(rep)
        sold, bld = detail(t, "sold_prices"), detail(t, "building")
        floor = bld.get("floor_m2")
        sales = [{"price_gbp": s.get("price"), "when": s.get("when"), "year": s.get("yr"),
                  "gbp_per_m2": round(s["price"] / floor) if floor and s.get("price") else None}
                 for s in sold.get("sales") or []]
        g = sold.get("growth") or None
        change = ({"from_gbp": g.get("from_price"), "from_year": g.get("from_yr"),
                   "to_gbp": g.get("to_price"), "to_year": g.get("to_yr"),
                   "gbp": g.get("delta"), "pct": g.get("pct")} if g else None)
        method = _join(sold.get("note"),
                       "Price per square metre divides each sale by the floor area on the home's "
                       "current certificate, so an extension built since a sale reads as a low "
                       "figure for that sale." if floor and sales else
                       None if floor else
                       "No floor area on an Energy Performance Certificate, so no price per square metre.")
        return _ok(akey=akey, address=_report_address(rep), postcode=rep.get("postcode"),
                   property_type=bld.get("property_type"), floor_area_m2=floor,
                   verdict=verdict(t, "sold_prices"), sales_on_record=len(sales), sales=sales,
                   change_first_to_last=change, method=method,
                   url=_home_url(akey, _report_label(rep)), sources=[core.SRC_HMLR, core.SRC_EPC])

    def _join(*parts) -> str:
        return " ".join(p.strip() for p in parts if p and p.strip())

    # ------------------------------------------------------------ energy
    @mcp.tool(title="Energy performance of one home", annotations=READ)
    @logged("energy_performance")
    async def energy_performance(akey: AKey, ctx: Context = None) -> dict:
        """A home's Energy Performance Certificate in plain terms: rating and score now and
        the rating it could reach, modelled running costs a year for heating, hot water and
        lighting, carbon dioxide, which parts of the building are weak (walls, roof,
        windows), and the assessor's own list of improvements with their indicative costs.
        Costs are the assessor's model at the prices of the certificate's date, not bills."""
        rep = await _get("/api/address-report", {"akey": akey}, ctx)
        t = _tiles(rep)
        en, bld = detail(t, "energy"), detail(t, "building")
        base = dict(akey=akey, address=_report_address(rep), postcode=rep.get("postcode"),
                    property_type=bld.get("property_type"), floor_area_m2=bld.get("floor_m2"),
                    verdict=verdict(t, "energy"),
                    url=_home_url(akey, _report_label(rep)), sources=[core.SRC_EPC])
        if not en.get("rating"):
            return _ok(**base, certificate=False,
                       reason="No Energy Performance Certificate on record for this home. "
                              "A home that has not been sold or let since 2008 may never have had one.")
        cost, pot, co2 = en.get("cost") or {}, en.get("potential") or {}, en.get("co2") or {}
        return _ok(
            **base, certificate=True, rating=en.get("rating"), score=en.get("score"),
            potential_rating=pot.get("rating"), potential_score=pot.get("score"),
            running_cost_gbp_per_year=cost.get("year"),
            running_cost_after_improvements_gbp_per_year=cost.get("potential"),
            possible_saving_gbp_per_year=cost.get("saving"),
            running_cost_split=[{"use": s.get("label"), "now_gbp": s.get("now"), "after_gbp": s.get("then")}
                                for s in cost.get("split") or []],
            co2_tonnes_per_year={"now": co2.get("now"), "after_improvements": co2.get("potential")},
            main_fuel=en.get("fuel"), mains_gas=en.get("mains_gas"),
            weakest=en.get("weakest") or [],
            building_elements=[{"part": e.get("label"), "rated": e.get("band"), "description": e.get("desc")}
                               for e in en.get("elements") or []],
            improvements=[{"what": i.get("what"), "indicative_cost": i.get("cost")}
                          for i in en.get("improvements") or []],
            inspected=en.get("inspected"), certificate_year=en.get("lodged_year"),
            method=en.get("note"))

    # ------------------------------------------------------------ price check
    @mcp.tool(title="Check a price against the district and the street", annotations=READ)
    @logged("price_check")
    async def price_check(
        akey: AKey,
        price_gbp: Annotated[int | None, Field(
            ge=10_000, le=100_000_000,
            description="The price to check, in pounds: an asking price, an offer, a valuation. "
                        "Leave out to get what the district's prices imply for a home this size "
                        "(and, when the home is advertised for sale, to check its asking price).")] = None,
        ctx: Context = None,
    ) -> dict:
        """Is a price in line? Works out the price per square metre from the home's EPC
        floor area and sets it against three things: what homes of the same type sold for
        per square metre in the same postcode district in the latest whole year, with the
        quartiles; recent sales on the home's own street or postcode; and the home's own
        last sale moved by the district's change since. Also gives the price range the
        district's quartiles imply for this floor area. Arithmetic on public medians, not a
        valuation, and the answer carries the caveats to pass on. Shares the address
        report's limit of 60 an hour per caller."""
        rep = await _get("/api/address-report", {"akey": akey}, ctx)
        t = _tiles(rep)
        bld, trend, local, market = (detail(t, "building"), detail(t, "price_trend"),
                                     detail(t, "local_market"), detail(t, "market_history"))
        sales = detail(t, "sold_prices").get("sales") or []
        floor, pt = bld.get("floor_m2"), trend.get("pt")
        oc = trend.get("outcode") or (rep.get("postcode") or "").split(" ")[0]
        last = sales[0] if sales else None
        asking = market.get("asking_price") if market.get("listing_status") == "active" else None
        price = price_gbp or asking
        base = dict(akey=akey, address=_report_address(rep), postcode=rep.get("postcode"),
                    property_type=bld.get("property_type"), floor_area_m2=floor,
                    last_sale=({"price_gbp": last.get("price"), "when": last.get("when")} if last else None),
                    url=_home_url(akey, _report_label(rep)), sources=[core.SRC_HMLR, core.SRC_EPC])
        if not floor:
            return _ok(**base, comparable=False, caveats=CAVEATS[:1],
                       reason="No floor area on an Energy Performance Certificate for this home, so a "
                              "price per square metre cannot be worked out. sale_history gives its past "
                              "prices and street_price_per_m2 what neighbours sold for.")
        tr = (await _get("/api/price-trends", {"oc": oc, "pt": pt, "factor": "all"}, ctx, soft=(404,))
              if pt and oc else {"ok": False})
        series = tr.get("baseline") or []
        if tr.get("ok") is False or not series:
            return _ok(**base, comparable=False, caveats=CAVEATS[:1],
                       reason=f"Too few sales of this type of home in {oc or 'the district'} with a floor "
                              "area on record to give a district median. street_price_per_m2 shows "
                              "what homes on the street sold for.")
        label = PT_LABEL.get(pt, "homes")
        latest = series[-1]
        med, q1, q3, yr = latest["med"], latest["q1"], latest["q3"], latest["yr"]
        district = {"outcode": oc, "property_type": label, "year": yr, "median_gbp_per_m2": med,
                    "lower_quartile_gbp_per_m2": q1, "upper_quartile_gbp_per_m2": q3, "sales": latest.get("n")}
        ytd, part = tr.get("baseline_partial"), tr.get("partial")
        if ytd and part:
            district["year_to_date"] = {"year": ytd.get("yr"), "months": part.get("months"),
                                        "median_gbp_per_m2": ytd.get("med"), "sales": ytd.get("n"),
                                        "change_vs_same_months_a_year_earlier_pct": ytd.get("pct")}
        implied = {"low_gbp": _round_k(q1 * floor), "mid_gbp": _round_k(med * floor),
                   "high_gbp": _round_k(q3 * floor),
                   "basis": f"{yr} lower quartile, median and upper quartile price per square metre "
                            f"for {label} in {oc}, multiplied by {floor} m2"}
        moved = None
        if last and last.get("price") and last.get("yr"):
            then = next((b for b in series if b.get("yr") == last["yr"]), None)
            if then and then.get("med"):
                moved = {"last_sale_gbp": last["price"], "last_sale": last.get("when"),
                         "district_median_then_gbp_per_m2": then["med"],
                         "district_median_now_gbp_per_m2": med,
                         "district_change_pct": round(100 * (med / then["med"] - 1), 1),
                         "moved_gbp": _round_k(last["price"] * med / then["med"])}
            else:
                moved = {"last_sale_gbp": last["price"], "last_sale": last.get("when"), "moved_gbp": None,
                         "reason": f"the district series runs from {series[0]['yr']} to {yr}"}
        street = ({"scope": local.get("scope_label"), "average_gbp_per_m2": local.get("avg_ppsm"),
                   "sized_sales": local.get("n_sized"), "since": local.get("from_year")}
                  if local.get("avg_ppsm") else None)
        out = dict(base, comparable=True, district=district, implied_range_for_this_floor_area=implied,
                   last_sale_moved_with_district=moved, street_recent_sales=street)
        caveats = list(CAVEATS)
        if price:
            ppsm = round(price / floor)
            pct = round(100 * (ppsm / med - 1), 1)
            where = ("below the lower quartile" if ppsm < q1 else
                     "between the lower quartile and the median" if ppsm < med else
                     "between the median and the upper quartile" if ppsm <= q3 else
                     "above the upper quartile")
            chk = {"price_gbp": price,
                   "price_is": ("the price given" if price_gbp else
                                "the asking price in the advert GetMovely has seen"),
                   "gbp_per_m2": ppsm, "vs_district_median_pct": pct, "position_in_district": where}
            if street and street["average_gbp_per_m2"]:
                chk["vs_street_recent_average_pct"] = round(100 * (ppsm / street["average_gbp_per_m2"] - 1), 1)
            if moved and moved.get("moved_gbp"):
                chk["vs_last_sale_moved_with_district_pct"] = round(100 * (price / moved["moved_gbp"] - 1), 1)
            out["price"] = chk
            gap = ("in line with" if abs(pct) < 0.5 else
                   f"{abs(pct):.0f}% {'above' if pct > 0 else 'below'}")
            out["summary"] = (f"£{price:,} is £{ppsm:,} per square metre, {gap} the {yr} median for "
                              f"{label} in {oc} (£{med:,}), {where} of that year's sales.")
            if not price_gbp:
                caveats.append("The asking price is from a public advert GetMovely has seen and can lag it.")
        else:
            out["summary"] = (f"{label.capitalize()} in {oc} sold for a median £{med:,} per square metre in "
                              f"{yr} (middle half £{q1:,} to £{q3:,}). At {floor} m2 that implies about "
                              f"£{implied['mid_gbp']:,}, within £{implied['low_gbp']:,} to "
                              f"£{implied['high_gbp']:,}.")
        out["caveats"] = caveats
        return _ok(**out)

    # ------------------------------------------------------------ compare homes
    @mcp.tool(title="Compare two to four homes", annotations=READ)
    @logged("compare_homes")
    async def compare_homes(
        akeys: Annotated[list[AKey], Field(min_length=2, max_length=4,
                                           description="Two to four address keys from search_addresses.")],
        ctx: Context = None,
    ) -> dict:
        """Two to four homes side by side, one row each: type and floor area, tenure, last
        sale and its price per square metre, energy rating and modelled running cost, what
        the street has sold for recently, the district trend, whether it is
        owner-occupied, whether it is listed for sale and its response likelihood (High =
        both), the nearest supermarket, primary school, station and park, recorded crime
        and air quality. Each row links its own report and the page
        that opens a letter to its owner. Counts as one address report per home against the
        60 an hour limit."""
        if len(set(akeys)) != len(akeys):
            raise ToolFailure("Each address key should appear once.")
        got = await _gather([_get("/api/address-report", {"akey": a}, ctx) for a in akeys]
                            + [_get("/api/address", {"akey": a}, ctx, soft=(404,)) for a in akeys])
        reports, cards = got[:len(akeys)], got[len(akeys):]
        rows, failed = [], []
        for ak, rep, card in zip(akeys, reports, cards):
            if isinstance(rep, Exception):
                failed.append(rep)
                rows.append({"akey": ak, "error": str(rep)})
                continue
            t = _tiles(rep)
            bld, en, local = detail(t, "building"), detail(t, "energy"), detail(t, "local_market")
            sales = detail(t, "sold_prices").get("sales") or []
            last, floor = (sales[0] if sales else {}), bld.get("floor_m2")
            crow = (card.get("row") or {}) if isinstance(card, dict) else {}
            facts = _home_facts(crow) if crow else {}
            row = {
                "akey": ak, "address": _report_address(rep), "postcode": rep.get("postcode"),
                "property_type": bld.get("property_type"), "floor_area_m2": floor,
                "tenure_at_last_sale": crow.get("tenure"), **facts,
                "last_sale_gbp": last.get("price"), "last_sale": last.get("when"),
                "last_sale_gbp_per_m2": round(last["price"] / floor) if floor and last.get("price") else None,
                "sales_on_record": len(sales),
                "energy_rating": en.get("rating"), "energy_score": en.get("score"),
                "energy_potential_rating": (en.get("potential") or {}).get("rating"),
                "energy_cost_gbp_per_year": (en.get("cost") or {}).get("year"),
                "street_recent_gbp_per_m2": local.get("avg_ppsm"), "street_scope": local.get("scope_label"),
                "district_trend": verdict(t, "price_trend"),
                "market": verdict(t, "market_history"),
                "nearest": {i.get("label"): {"name": i.get("name"), "metres": i.get("dist")}
                            for i in detail(t, "nearby").get("items") or []},
                "crime": verdict(t, "crime"), "crime_flagged": (t.get("crime") or {}).get("status") == "flag",
                "air_quality": verdict(t, "air"),
                "summary": rep.get("summary"),
                "url": _home_url(ak, _report_label(rep)),
            }
            if crow:
                row["contact_owner_url"] = _contact_url(ak, _report_label(rep), _state(crow))
            if bld.get("bedrooms"):
                row["bedrooms"] = bld["bedrooms"]
            rows.append(row)
        if len(failed) == len(akeys):
            raise failed[0]
        return _ok(homes=rows, compared=len(akeys) - len(failed),
                   method="Each row is that home's address report in brief. Price per square metre uses "
                          "the floor area on the current Energy Performance Certificate. Tenure is as "
                          "recorded at the last sale.",
                   about_these_fields=core.ABOUT_HOME_FACTS,
                   caveats=CAVEATS[1:], url=f"{PUBLIC}/tools/address-report",
                   sources=[core.SRC_HMLR, core.SRC_EPC, core.SRC_OS, core.SRC_POI, core.SRC_CRIME_AIR])

    # ------------------------------------------------------------ the founding trick
    @mcp.tool(title="Find the address behind a listing from its sale history", annotations=READ)
    @logged("find_address_from_sale_history")
    async def find_address_from_sale_history(
        area: Annotated[str, Field(
            min_length=2, max_length=160,
            description="Where the listing says the home is: a street with its district ('Merton Road, "
                        "SW19'), a postcode or its first half ('SW19 1'), or a town.")],
        sales: Annotated[list[str], Field(
            min_length=1, max_length=6,
            description="Past sales shown on the listing, each as 'price:year' in pounds, for example "
                        "['450000:2019', '310000:2012']. More sales narrow it faster.")],
        match: Annotated[Literal["any", "last", "only"], Field(
            description="'any' (default): the home has these sales, possibly among others. 'last': the "
                        "newest sale given is the home's most recent sale. 'only': the home has exactly "
                        "these sales and no others.")] = "any",
        ctx: Context = None,
    ) -> dict:
        """Property portals often hide the house number but show the home's past sale
        prices. HM Land Registry records every sale by exact address, so the sale history
        identifies the home: give the area and the sales, get the address or addresses that
        match. One match is the answer. Several means add another sale or a tighter area.
        None means check the year (portals show the year the sale completed) or widen the
        area. The returned `akey` works with every per-address tool."""
        data = await _get("/api/addresses", {"q": area, "pairs": core._sale_pairs(sales),
                                             "pairs_mode": match, "sort": "recent"}, ctx)
        guide = f"{PUBLIC}/guides/find-address-of-rightmove-listing"
        if data.get("streets") is not None:
            return _ok(matches=None, area=area, sales=sales,
                       outcome="That street name exists in several places. Add the town or the "
                               "postcode district and try again.",
                       places=[{"street": (s.get("street") or "").title(), "outcode": s.get("outcode"),
                                "homes": s.get("n")} for s in data["streets"][:20]],
                       guide=guide, url=guide)
        if data.get("need_filter"):
            raise ToolFailure("Add a street, postcode or town to search in.")
        raw = data.get("rows") or []
        total = data.get("total") or len(raw)
        rows = [core._address_row(r) for r in raw[:20]]
        if total == 1:
            outcome = "One home in that area has this sale history: this is the address."
        elif total:
            outcome = (f"{total} homes in that area have this sale history. Add another past sale, "
                       "or narrow the area to the street or the postcode.")
        else:
            outcome = ("No home in that area has this sale history. Check the year (the year the "
                       "sale completed), try match='any', or widen the area to the whole district.")
        return _ok(matches=total, area=area, sales=sales, match=match, outcome=outcome, rows=rows,
                   guide=guide, url=rows[0]["url"] if total == 1 and rows else guide,
                   sources=[core.SRC_HMLR, core.SRC_EPC, core.SRC_OS])
