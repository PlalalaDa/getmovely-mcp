"""GetMovely MCP server: the guides and the prompts added in 0.2.0.

Guides. GetMovely publishes plain-English guides (writing to an owner and the
law on it, buying or selling without an agent, who owns a property, how often
sales fall through). search_guides and read_guide give them to an assistant as
text with their cited sources, and getmovely://guides exposes the same as
resources. The content comes from Flask's /api/guides and /api/guide, which
enforce the publish gate; this module only turns the page's HTML into Markdown.

Prompts. Six ready-made requests (a viewing brief, a price check, finding the
address behind a listing, comparing districts on a budget, an energy upgrade
plan, a first letter to an owner). A client shows them as slash commands or a
prompt menu; each one only tells the assistant which of this server's tools to
use and what to hand back, with the same caveats the tools carry.

Loaded by gm_mcp.py through register(core); see gm_mcp_homes.py for why this
module neither imports gm_mcp nor uses stringified annotations.
"""
import re
import time
from html.parser import HTMLParser
from typing import Annotated

from pydantic import Field
from mcp.server.mcpserver import Context

GUIDES_TTL = 600
_WORD = re.compile(r"[a-z0-9]{3,}")
_COMMON = {"the", "and", "for", "you", "your", "how", "what", "who", "can", "does", "are", "with",
           "that", "this", "from", "about", "house", "home", "property", "there", "have", "has"}


class _Markdown(HTMLParser):
    """The guides' HTML (p, h2, ul/ol/li, a, strong/em, table, br, div) as
    Markdown. Small on purpose: the bodies are our own, written to one house
    style, so this handles the sixteen tags they use and nothing else."""

    def __init__(self, base: str):
        super().__init__(convert_charrefs=True)
        self.base = base
        self.out: list[str] = []
        self.lists: list[list] = []          # [kind, counter]
        self.link: list | None = None        # [href, [text]]
        self.table: list[list[str]] | None = None
        self.row: list[str] | None = None
        self.cell: list[str] | None = None
        self.skip = 0                        # inside <script>/<style>: emit nothing

    def _emit(self, text: str) -> None:
        if self.skip:
            return
        if self.cell is not None:
            self.cell.append(text)
        elif self.link is not None:
            self.link[1].append(text)
        else:
            self.out.append(text)

    def handle_starttag(self, tag, attrs):
        a = dict(attrs)
        if tag in ("script", "style"):
            self.skip += 1
        elif tag in ("p", "div"):
            self._emit("\n\n")
        elif tag == "h2":
            self._emit("\n\n## ")
        elif tag == "h3":
            self._emit("\n\n### ")
        elif tag == "br":
            self._emit("\n")
        elif tag in ("ul", "ol"):
            self.lists.append([tag, 0])
            self._emit("\n")
        elif tag == "li":
            kind = self.lists[-1] if self.lists else ["ul", 0]
            kind[1] += 1
            self._emit("\n" + ("- " if kind[0] == "ul" else f"{kind[1]}. "))
        elif tag in ("strong", "b"):
            self._emit("**")
        elif tag in ("em", "i"):
            self._emit("*")
        elif tag == "a":
            href = (a.get("href") or "").strip()
            if href.startswith("/"):
                href = self.base + href
            self.link = [href, []]
        elif tag == "table":
            self.table = []
        elif tag == "tr":
            self.row = []
        elif tag in ("td", "th"):
            self.cell = []

    def handle_endtag(self, tag):
        if tag in ("script", "style"):
            self.skip = max(0, self.skip - 1)
        elif tag in ("strong", "b"):
            self._emit("**")
        elif tag in ("em", "i"):
            self._emit("*")
        elif tag == "a" and self.link is not None:
            href, parts = self.link
            self.link = None
            text = "".join(parts).strip()
            self._emit(f"[{text}]({href})" if href and text else text)
        elif tag in ("ul", "ol") and self.lists:
            self.lists.pop()
            self._emit("\n")
        elif tag in ("td", "th") and self.cell is not None and self.row is not None:
            self.row.append(re.sub(r"\s+", " ", "".join(self.cell)).strip().replace("|", "/"))
            self.cell = None
        elif tag == "tr" and self.row is not None and self.table is not None:
            self.table.append(self.row)
            self.row = None
        elif tag == "table" and self.table is not None:
            rows, self.table = self.table, None
            if rows:
                width = max(len(r) for r in rows)
                lines = ["| " + " | ".join(r + [""] * (width - len(r))) + " |" for r in rows]
                lines.insert(1, "|" + " --- |" * width)
                self._emit("\n\n" + "\n".join(lines) + "\n\n")

    def handle_data(self, data):
        self._emit(re.sub(r"\s+", " ", data))


def to_markdown(html_text: str, base: str) -> str:
    p = _Markdown(base)
    p.feed(html_text or "")
    p.close()
    text = "".join(p.out)
    text = re.sub(r"[ \t]+\n", "\n", text)
    text = re.sub(r"\n[ \t]+", "\n", text)
    return re.sub(r"\n{3,}", "\n\n", text).strip()


def guide_markdown(g: dict, base: str) -> str:
    """One guide as a Markdown document: title, dates, summary, body, the
    questions it answers, the sources it cites, the related tool, the URL."""
    parts = [f"# {g.get('title')}",
             f"*GetMovely guide. Published {g.get('published')}, updated {g.get('updated')}. "
             f"General information, not legal advice. {g.get('url')}*",
             f"**In short:** {g.get('summary')}",
             to_markdown(g.get("body_html") or "", base)]
    if g.get("faq"):
        parts.append("## Questions and answers\n\n" + "\n\n".join(
            f"**{qa.get('q')}**\n{qa.get('a')}" for qa in g["faq"]))
    if g.get("sources"):
        parts.append("## Sources\n\n" + "\n".join(
            f"- [{s.get('name')}]({s.get('url')})" + (f": {s.get('note')}" if s.get("note") else "")
            for s in g["sources"]))
    tool = g.get("tool")
    if tool:
        parts.append(f"Related free tool: [{tool.get('name')}]({tool.get('url')}), which {tool.get('line')}")
    return "\n\n".join(p for p in parts if p)


def _score(query: str, g: dict) -> int:
    words = [w for w in _WORD.findall(query.lower()) if w not in _COMMON]
    if not words:
        return 0
    title = (g.get("title") or "").lower()
    questions = " ".join(g.get("questions") or []).lower()
    rest = f"{g.get('summary') or ''} {g.get('description') or ''} {g.get('slug') or ''}".lower()
    return sum(3 * (w in title) + 2 * (w in questions) + (w in rest) for w in words)


def register(core):
    mcp, READ, logged = core.mcp, core.READ, core.logged
    _get, _finish, ToolFailure, PUBLIC = core._get, core._finish, core.ToolFailure, core.PUBLIC
    CREDIT = ("GetMovely guide, getmovely.co.uk/guides. General information, not legal advice; "
              "the primary sources each guide relies on are listed with it.")
    index_cache: list = [0.0, []]

    async def guide_index(ctx) -> list[dict]:
        if index_cache[1] and time.time() - index_cache[0] < GUIDES_TTL:
            return index_cache[1]
        data = await _get("/api/guides", {}, ctx)
        index_cache[0], index_cache[1] = time.time(), data.get("guides") or []
        return index_cache[1]

    def brief(g: dict) -> dict:
        return {"slug": g.get("slug"), "title": g.get("title"), "summary": g.get("summary"),
                "answers": g.get("questions") or [], "for": g.get("side"), "updated": g.get("updated"),
                "url": g.get("url")}

    @mcp.tool(title="Search the guides", annotations=READ)
    @logged("search_guides")
    async def search_guides(
        query: Annotated[str | None, Field(
            max_length=160,
            description="What the person wants to know, in plain words: 'is it legal to write to a "
                        "homeowner', 'who owns the freehold', 'how many house sales fall through'. "
                        "Leave out to list every guide.")] = None,
        ctx: Context = None,
    ) -> dict:
        """GetMovely's plain-English guides to buying and selling homes in England and
        Wales: contacting an owner directly and the law on it, buying or selling without an
        estate agent, finding who owns a property or the address behind a listing, how
        often sales fall through, speeding a sale up, best and final offers, stopping
        unwanted letters. Returns the closest guides with a one-paragraph summary and the
        questions each answers; read_guide gives the full text with its sources."""
        guides = await guide_index(ctx)
        if query and query.strip():
            ranked = sorted(((_score(query, g), g) for g in guides), key=lambda x: -x[0])
            hits = [g for s, g in ranked if s > 0][:6]
            matched = bool(hits)
        else:
            hits, matched = guides, None
        return _finish({"query": query, "matched": matched,
                       "hint": (None if matched is not False else
                                "No guide matches those words closely; these are all of them."),
                       "guides": [brief(g) for g in (hits or guides)], "guides_total": len(guides),
                       "url": f"{PUBLIC}/guides", "sources": [CREDIT], "note": core.ATTRIBUTION_NOTE})

    Slug = Annotated[str, Field(min_length=3, max_length=120, pattern=r"^[a-z0-9-]+$",
                                description="Guide slug from search_guides, for example "
                                            "'is-it-legal-to-write-to-a-homeowner'.")]

    async def fetch_guide(slug: str, ctx) -> dict:
        data = await _get("/api/guide", {"slug": slug}, ctx, soft=(404,))
        if data.get("ok") is False or not data.get("guide"):
            core._note("guide not found")
            raise ToolFailure(f"No guide with the slug {slug!r}. search_guides lists them.")
        return data["guide"]

    @mcp.tool(title="Read a guide", annotations=READ)
    @logged("read_guide")
    async def read_guide(slug: Slug, ctx: Context = None) -> dict:
        """The full text of one GetMovely guide as Markdown, with the questions it
        answers, the primary sources it cites (GOV.UK, HM Land Registry, the ICO, The
        Property Ombudsman and others) and the date it was last updated. General
        information, not legal advice. Cite the guide's `url` when you use it."""
        g = await fetch_guide(slug, ctx)
        return _finish({"slug": g.get("slug"), "title": g.get("title"), "summary": g.get("summary"),
                       "published": g.get("published"), "updated": g.get("updated"),
                       "markdown": guide_markdown(g, PUBLIC),
                       "references": g.get("sources") or [], "related_tool": g.get("tool"),
                       "url": g.get("url"), "sources": [CREDIT], "note": core.ATTRIBUTION_NOTE})

    @mcp.resource("getmovely://guides", mime_type="application/json", title="Guides index")
    async def guides_resource() -> dict:
        """Every published GetMovely guide: slug, title, summary, the questions it answers,
        its page and its Markdown resource."""
        guides = await guide_index(None)
        return {"guides": [dict(brief(g), resource=f"getmovely://guides/{g.get('slug')}") for g in guides],
                "credit": CREDIT}

    @mcp.resource("getmovely://guides/{slug}", mime_type="text/markdown", title="Guide")
    async def guide_resource(slug: str, ctx: Context = None) -> str:
        """One GetMovely guide as Markdown, with its questions, sources and page URL.
        Slugs are listed in getmovely://guides."""
        if not re.fullmatch(r"[a-z0-9-]{3,120}", slug or ""):
            raise ValueError("unknown guide; slugs are listed in getmovely://guides")
        return guide_markdown(await fetch_guide(slug, ctx), PUBLIC)

    # ------------------------------------------------------------ prompts
    LINKS = ("Link the GetMovely page (the `url` in each tool answer) beside every figure you quote, "
             "and say where the data comes from (HM Land Registry, Energy Performance Certificates).")

    @mcp.prompt(title="Viewing brief for a home")
    def viewing_brief(address: str) -> str:
        """Everything worth knowing before viewing a home: sale history, size, energy,
        price against the area, what is nearby, and the questions to ask."""
        return (f"I am going to view this home: {address}\n\n"
                "Using the GetMovely tools, prepare a one-page viewing brief.\n"
                "1. search_addresses to find the exact home (ask me if several match).\n"
                "2. address_report for the full picture, and price_check for how its last sale or "
                "asking price sits against the district and the street.\n"
                "3. whats_nearby with closest='school_primary' and closest='rail_station' if those "
                "matter for this kind of home.\n\n"
                "Then write: what it is (type, size, age if known, tenure), whether it is advertised "
                "for sale and whether the energy certificate records it as owner-occupied, what it "
                "has sold for and when, what the price per square metre says, running costs and the "
                "weak points of the building from the energy certificate, what is within a walk, "
                "anything flagged. "
                "Finish with five specific questions to ask at the viewing that follow from the "
                "data, not generic ones. " + LINKS + " Say plainly that the price comparison is "
                "arithmetic on public records, not a valuation.")

    @mcp.prompt(title="Is this price fair?")
    def fair_price(address: str, price: str = "") -> str:
        """Set an asking price or an offer against what the district and the street
        actually sold for, with the caveats."""
        about = f"the price £{price.lstrip('£')}" if price.strip() else "its asking price or last sale"
        return (f"Help me judge {about} for this home: {address}\n\n"
                "Use search_addresses to find the home, then price_check"
                + (" with that price" if price.strip() else "") + ", and street_price_per_m2 for the "
                "individual sales nearby. Tell me: the price per square metre, where it sits against "
                "the district's median and quartiles for the same type of home, how it compares with "
                "the street's recent sales and with the home's own last sale moved by the district's "
                "change, and what would explain a gap (size of the sample, an outlier sale, a home "
                "extended since its certificate, the mix of homes sold). Give a range, not a single "
                "number, and repeat the caveats the tool returns. " + LINKS)

    @mcp.prompt(title="Find the address behind a listing")
    def find_address_behind_listing(area: str, sale_history: str) -> str:
        """A portal hides the house number but shows past sale prices: use them to find
        the exact address."""
        return (f"A property listing in {area} hides the exact address but shows this sale history: "
                f"{sale_history}\n\n"
                "Turn each past sale into 'price:year' and call find_address_from_sale_history with "
                "the area. If one home matches, tell me the address and confirm it with sale_history. "
                "If several match, tell me which further detail from the listing would separate them. "
                "If none match, try match='any', the neighbouring year, or the wider district, and "
                "say what you tried. " + LINKS)

    @mcp.prompt(title="Compare areas for a budget")
    def compare_areas_for_budget(districts: str, property_type: str = "terraced house",
                                 floor_area_m2: str = "") -> str:
        """Set postcode districts side by side for one kind of home, optionally at a
        floor area, to see what a budget buys where."""
        size = f" of about {floor_area_m2} square metres" if floor_area_m2.strip() else ""
        return (f"Compare these postcode districts for a {property_type}{size}: {districts}\n\n"
                "Call compare_areas with the districts, the matching property type (D detached, S "
                "semi-detached, T terraced, F flat)" + (" and the floor area" if size else "") +
                ". Give me a table: median price per square metre, what the middle half of sales "
                "paid, how many sales that rests on, the change since 2015 and how this year is "
                "running. Then one paragraph on what the figures cannot tell me. If a district has too "
                "few sales of that type, say so rather than leaving it out. " + LINKS)

    @mcp.prompt(title="Energy upgrade plan for a home")
    def energy_upgrade_plan(address: str) -> str:
        """Read a home's energy certificate and turn it into an ordered list of
        improvements with costs and savings."""
        return (f"Make an energy upgrade plan for this home: {address}\n\n"
                "Use search_addresses, then energy_performance. List the assessor's recommended "
                "improvements in order of saving per pound using the indicative costs given, name the "
                "weakest parts of the building, and say what rating the home could reach. Be clear "
                "that the costs and savings are the assessor's model at the prices of the "
                "certificate's date, give that date, and say when the certificate is old enough that "
                "a new assessment is worth having. " + LINKS)

    @mcp.prompt(title="Write a first letter to the owner of a home")
    def letter_to_owner(address: str, about_me: str = "") -> str:
        """Draft one respectful letter to the owner of a home that may not be for sale,
        following GetMovely's guide and the law on unsolicited letters."""
        me = f"About me: {about_me}\n\n" if about_me.strip() else ""
        return (f"I would like to write to the owner of this home to ask whether they would consider "
                f"selling: {address}\n\n{me}"
                "First call read_guide for 'is-it-legal-to-write-to-a-homeowner' and "
                "'letter-to-homeowner-asking-to-buy-house' and follow what they say. Use "
                "search_addresses and address_report so the letter is about this home and not a "
                "template, but do not quote the owner's purchase price back at them. Tell me first "
                "what the report's `home` block says: whether the energy certificate records the home "
                "as owner-occupied (if public records do not say, the letter may reach a tenant, so "
                "it should ask to be passed to the owner) and whether it is already advertised for "
                "sale (then the letter should say I have seen that).\n\n"
                "Write one letter: addressed to 'The Homeowner' (never guess a name), who I am and "
                "why this home, that there is no obligation and no agent involved, how to reply, and "
                "a plain line saying I will not write again if I hear nothing or if they would rather "
                "not be contacted. One page, no pressure, no claim about what the home is worth. "
                "Then tell me the two ways to send it: print and post it myself, or open the "
                "report's `contact_owner_url`, which opens the letter to this home on GetMovely "
                "(free; sending needs a free account) and lets the owner reply without sharing "
                "their details. The letter is sent from that page.")
