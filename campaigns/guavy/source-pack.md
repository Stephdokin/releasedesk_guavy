# Source pack: Guavy

Every factual claim in any post must trace to a line in this file. If it is not
written here, the desk does not know it and will not write it. That is the whole
point of the pack, and for a market data product it is not optional.

## What Guavy is

Guavy is the market intelligence layer for the agentic era. It continuously
turns market information into structured, scored intelligence: market sentiment,
curated news, and trend signals, across four live markets: Crypto, US Equities,
FX, and Commodities. Each record is scored as it is processed and identifies
which instruments it affects, and every record is time-stamped, building a
point-in-time history of what Guavy identified at the time. It is delivered
through a REST API and native MCP, and is built for developers and trading-bot
builders, quant and fund teams, and agentic applications. Guavy Enterprise
applies the same engine to a universe an organization defines: the companies,
markets, regulators, suppliers, industries, themes, and information sources that
matter to it.

## What it is not

Guavy is not an adviser and does not tell anyone what to trade. In its published
words, "Guavy does not provide investment, financial, legal, or tax advice;
Guavy is not a broker, dealer, exchange, or investment adviser. Content is for
informational purposes only and may be inaccurate, delayed, incomplete, or
change without notice." Guavy is not registered with any securities regulator.
It is a second opinion and a risk-assessment layer for a user's own research.
Nothing it publishes is a recommendation, an endorsement of a strategy, or a
promise of any result.

## Confirmed facts

- Four live markets: Crypto, US Equities, FX, Commodities. House style is "US
  Equities", never "Stocks".
- Delivered over a REST API and native MCP.
- Crypto point-in-time history and backtests trace back to July 2024. Crypto
  only. There is no comparable history for US Equities, FX or Commodities, and
  none may be claimed.
- The trade simulator runs aggressive and conservative strategies.
- Guavy Enterprise applies the same engine to a customer-defined universe.
- Guavy is not registered with any securities regulator.
- The full disclaimer wording is counsel-reviewed and cleared for the US,
  Canada, UK, Australia and Singapore.

## Numbers

Any figure that goes in a post belongs here first, with where it came from and
the date it was true. Live market figures (a sentiment reading, a brief's
price move) come from the brief or article the post is about, not from here.

### The general-purpose LLM case study

Source: "Why Guavy vs. a General-Purpose LLM? 122x the Data, 11x the
Precision", Guavy blog, published 26 August 2026,
https://guavy.com/blog/why-guavy-vs-general-purpose-llm. Study run 11 August
2026.

What was measured: one Bitcoin morning-brief prompt, sent to five AI tools,
and how much evidence each answered from. The prompt asked for latest
sentiment counts, the current trade action for the aggressive and conservative
simulator, the most impactful news brief of the last 24 hours, and the most
recent trend strength and direction.

| Tool | Answered from | Margin of error |
|---|---|---|
| Copilot | 6 headlines | ±40.0 pts |
| ChatGPT | 10 headlines | ±31.0 pts |
| Grok | 69 sources | ±11.8 pts |
| Gemini | no answer given | n/a |
| Claude + Guavy MCP | 1,220 mentions | ±2.8 pts |

Methodology, as published: margins of error are 95% confidence intervals on a
proportion at the worst-case split, 1.96 × √(0.25/n), in percentage points.
"122x the data" is 1,220 against ChatGPT's 10; because error falls with the
square root of the sample, 122 times the data is 11 times the precision.

Keep its scope. This measures sample size and sampling error on one prompt on
one day. It says nothing about whether any sentiment reading was right about
the market. Grok's 69 sources and Gemini's non-answer are part of the result:
do not describe the general-purpose tools as all answering from 6 to 10
headlines.

### Dated milestones, from Guavy's own announcements

- 15 December 2024: Guavy introduced. (Guavy blog, "Introducing Guavy")
- 10 December 2025: iOS app launched. (Guavy blog)
- 8 January 2026: AI-native crypto API launched. (Guavy blog)
- 21 April 2026: MCP integration launched. (Guavy blog)
- 15 July 2026: Guavy 3.0 expands beyond crypto to Commodities and FX. (Guavy
  blog)
- 18 August 2026: expansion to US Equities. (Guavy blog)

**Regulated speech.** The rules for what may be said about markets,
performance and backtests are in `profiles.yaml` under `voice.claims`. They are
binding, and they win over anything in this pack.

## People and credits

- Instagram @guavysentiment and the LinkedIn company page "Guavy Inc." are both
  owned channels, confirmed September 2026.
- TODO: who is credited on posts, and whether any individual is named at all.

## Provenance of the claims rules

Recorded 23 September 2026, so a later reader can tell decision from inference.

- Category line, "structured, scored intelligence", the four markets, and the
  per-record scoring language: Guavy.com Web Copy Review, 23 Sep 2026 (Donna
  Tilden).
- Enterprise universe language, and the instruction that accumulating history
  must not imply Guavy becomes more accurate over time: DMT Guavy Enterprise Web
  Copy, 23 Sep 2026.
- "US Equities" over "Stocks", and the counsel-reviewed disclaimer wording:
  Guavy Pricing Web Copy, 23 Sep 2026.
- No-advice language, "not a broker, dealer, exchange, or investment adviser",
  backtests as informational, and "no warranty ... accurate, or profitable":
  Terms of Use, guavy.com/terms, updated 23 June 2026.
- Not registered, and the second-opinion positioning: project product-scope
  record.
- Jurisdictions investigated and the disclaimer found satisfactory by counsel
  (US, Canada, UK, Australia, Singapore); "trend signals", "news" and
  "sentiment" authorized as product vocabulary; Instagram confirmed as an owned
  channel: Stephen King, CGO, September 2026.

## Links

- Home: https://guavy.com
- Blog: https://guavy.com/blog
- Terms of Use: https://guavy.com/terms
- The LLM case study: https://guavy.com/blog/why-guavy-vs-general-purpose-llm
