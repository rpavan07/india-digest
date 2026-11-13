# India, region by region

A daily news digest that gives every region of India equal space, so the East,
Northeast and Central states aren't drowned out by Delhi, Mumbai and Bengaluru.

```
py digest.py --open      # or double-click run.bat
```

Output: `output/digest.html`. Uses only the Python standard library, with no API keys.

## How it works
1. Fetches the last ~30h of Google News India results for **every state/UT** (35 feeds; queries in `config.json`).
2. Groups headlines about the same event, so one crash covered by 29 outlets shows up as one story.
3. Gives each region a fixed number of slots (`max_stories_per_region`) and fills them
   **round-robin by state**, so Mizoram gets a slot before Delhi's 5th story.
4. Ranks stories by number of outlets plus freshness. For each story it shows the **most neutrally
   worded headline** and lets you compare how the other outlets framed it.

## Bias measurements (bottom of the page)
| Measure | How it's calculated | What it tells you |
|---|---|---|
| **Regional attention** | National top/NATION headlines tagged by state names and cities; region share ÷ population share (Census 2011). Saved to `output/history.json` and summed over up to 30 days. | How lopsided a normal national feed is |
| **Blind spots** | States with many local stories but few national headlines | What you'd miss without this digest |
| **Charged wording** | Headline has loaded words (`LOADED` list in digest.py), ALL-CAPS words, or `!` | Sensational framing, per story and per outlet |
| **Single source** | Only one outlet reported the story | Worth checking before you trust it |
| **Outlet concentration** | Share of a region's articles from its biggest outlet | Whether one outlet dominates your view of a region |
| **Political lean** (optional) | Your own labels in `config.json` → `source_lean` | Spread of leans covering each story |

### Why political lean isn't pre-filled
Political-lean ratings for Indian outlets are contested, and any built-in list would just be someone's opinion.
If you want to add them, use your own judgment or a published rating such as Media Bias/Fact Check,
which covers some Indian outlets:

```json
"source_lean": { "Outlet Name": "pro-govt", "Other Outlet": "opposition-leaning" }
```

Once labels are set, each story shows which leans covered it. A story covered only by one side is a coverage blindspot.

## Limits
- Google News RSS returns at most 100 items per query, so big states get truncated ("95+" in the table).
- Headlines only, no article text, so the tone check is a keyword heuristic.
- English only (`language` in config). Hindi or regional-language feeds would need a separate word list.
- Google News RSS is for personal, non-commercial use.
