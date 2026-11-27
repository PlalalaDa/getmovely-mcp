# GetMovely MCP server

[![GetMovely MCP connector](https://glama.ai/mcp/connectors/uk.co.getmovely/getmovely/badges/score.svg)](https://glama.ai/mcp/connectors/uk.co.getmovely/getmovely)

UK property data for your AI assistant: every home in England and Wales.
Ask it to find homes for sale that fit a budget or a description in your own
words, list the homes on a street
that are **not** on the market and whose owners live there, show what sold
nearby, check whether an asking price is in line, work out stamp duty, or
read the full report on one address. Free, read-only, no account, no API key.

**Server URL:** `https://getmovely.co.uk/mcp` (Streamable HTTP, no authentication)
· Registry name `uk.co.getmovely/getmovely` · Website https://getmovely.co.uk

## Connect

| Client | How |
|---|---|
| Claude (claude.ai, desktop) | Settings › Connectors › Add custom connector › URL `https://getmovely.co.uk/mcp` |
| Claude Code | `claude mcp add --transport http getmovely https://getmovely.co.uk/mcp` (or use this repo's `.mcp.json`) |
| ChatGPT | Open `chatgpt.com/plugins`, press the plus button, choose "Add custom MCP server" and give the URL above (whether the option appears depends on your plan and workspace settings) |
| Cursor, Windsurf | Add a remote MCP server of type `http` with the URL above |
| VS Code | Copy `.vscode/mcp.json` from this repo into your workspace |
| Gemini CLI | `gemini extensions install https://github.com/PlalalaDa/getmovely-mcp` |
| OpenClaw | `openclaw mcp add getmovely --url https://getmovely.co.uk/mcp --transport streamable-http` |

## What you can ask

- "Find me a 3-bed house under £400k in BS7."
- "A riverside flat with a balcony in London, under £600k."
- "Which owner-occupied houses on Elm Road, BS7 haven't sold since 2005?"
- "What sold in SW19 this year for under £600k?"
- "Is £525,000 a fair price for 12 Example Street?" (any real address)
- "Anything to worry about before I offer on 12 Example Street? Flood, planning, schools, broadband."
- "How quickly are homes selling in SW19 right now?"
- "How much stamp duty on £450,000 as a first-time buyer?"
- "Compare BS7, M20 and SW19 for terraced houses at 90 m²."
- "This listing hides the house number but shows it sold for £310k in 2012 and £450k in 2019. Which house is it?"

## Tools (26)

| Group | Tools |
|---|---|
| Search like a buyer | `search_homes_for_sale`, `describe_homes_for_sale`, `find_homes_not_for_sale`, `recent_sales`, `stamp_duty`, `draft_letter` |
| Find a home | `search_addresses`, `find_address_from_sale_history`, `get_address` |
| One home | `address_report`, `buyer_checks`, `negotiation_brief`, `sale_history`, `price_check`, `energy_performance`, `compare_homes`, `whats_nearby`, `tenure_check`, `house_age`, `street_price_per_m2` |
| An area | `area_overview`, `compare_areas`, `price_trends`, `district_statistics` |
| Guides | `search_guides`, `read_guide` |

Plus six prompts (viewing brief, fair price, find the address behind a listing,
compare areas on a budget, energy upgrade plan, letter to an owner) and the
research tables and guides as resources.

Every tool is read-only. `draft_letter` returns a link that opens a letter to a
home's owner on GetMovely with the agreed text filled in; the person reads it
and sends it there themselves. Nothing is sent by the server.

## Where the data comes from

HM Land Registry Price Paid Data, the Energy Performance Certificate register,
official open data for transport, schools and their results, crime, air quality,
flood risk, broadband (Ofcom), planning designations (listed buildings,
conservation areas, green belt, national parks and landscapes), household
energy use, deprivation and the 2021 Census, private rents and council tax,
GetMovely's own record of homes advertised for sale, and GetMovely's published
research. Each answer says what it is built from, names its sources with their
attribution lines and carries its caveats (sale-price arithmetic is not a
valuation). The main sources and their licences: https://getmovely.co.uk/privacy

## This code

**There is nothing to install or run.** GetMovely is a hosted (remote) server:
connect your client to `https://getmovely.co.uk/mcp` as in the table above. No
Python, no Node, no Docker, no API key.

This repository is the source of the hosted server, published so anyone can
read exactly what it does with a request. It is a thin layer over the
GetMovely website's own JSON routes, which hold the data and the rate limits;
cloned and run locally it has nothing to answer from, so it is not a way to
self-host. Directory manifests here (`server.json`, `lhm.plugin.json`,
`gemini-extension.json`, `.mcp.json`) all point at the hosted URL.

- `gm_mcp.py`: the core (server, backend access, per-caller budgets, the call
  log, the original tools, the page at `GET /mcp`, `/mcp/health`).
- `gm_mcp_homes.py`, `gm_mcp_areas.py`, `gm_mcp_search.py`,
  `gm_mcp_content.py`: per-home, area, buyer's-search and guide tools, and the prompts.
- `server.json`: the official MCP Registry entry.
- Official Python SDK (`mcp` 2.3), Python 3.14, Streamable HTTP, stateless.

What the log keeps: per call the tool, success, time, user agent, protocol
version, country, and a caller code that is an HMAC of the IP under a random
key replaced daily, so codes cannot be traced back to an address afterwards.
Lines are deleted after 90 days.

Fair use: about 90 requests a minute and 1,500 a day per connection; address
reports are 60 an hour, as on the website.

## Licence

Code: MIT. The data the hosted server returns is under the licences listed at
https://getmovely.co.uk/privacy. Security reports: see `SECURITY.md`.
