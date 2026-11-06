## This skill is the ability to deduce whether an individual credit (almost always a limited liability company or a legal entity that is similar) can repay its debts. If the company has a high likelihood of being unable to repay its debts, we will try to decide either when the company will first miss a payment on its debt, or when the company will decide to enter a bankruptcy-like legal regime to reduce its debt. This is because credit default swaps (CDS) "trigger" when one of these two events happens, either entering bankruptcy or failing to pay interest or principal on its debt obligations.




### Process
1) Obtain financials.
a) For SEC filers in the USA, all of the information can be obtained via the SEC EDGAR database. All that is needed is a CIK number. To download credit agreements, indentures and amendments into `companies\{ticker}\filings\` (with a manifest of source URLs), run on a machine that can reach the S: drive: `python download_filings.py {TICKER} --user-agent "Your Name you@firm.com"` (script is in this folder; add `--dry-run` to list without downloading). The SEC requires the contact email in the User-Agent. Then read each document and record terms in `{ticker}_credit_terms.md` (see `companies\xrx\xrx_credit_terms.md` for the format).
b) For private companies, we will either need access to a "data room" (just a password protected website) or I can upload the needed files to a local location.

---

## Skill 1: Cash from Operations (CFO) and Free Cash Flow (FCF)

### Folder convention
- Each company lives in `companies\{ticker}\` (e.g. `companies\xrx\`). Filings (10-Q, 10-K, 8-K earnings releases, PDFs or HTML) are saved there.
- Output goes in the same folder as `{ticker}_fcf.md`. If the file exists, update it (add the new period); do not overwrite prior periods.

### Definitions (reference, so nothing needs to be memorized)
- **CFO** = "Net cash provided by (used in) operating activities", the subtotal on the cash flow statement. A GAAP line (ASC 230). It is cash after interest paid and taxes paid (US GAAP), and after working capital changes. It is before capex, debt principal and dividends.
- **FCF** (as companies and data vendors usually mean it) = CFO - capex. Not FCFF and not FCFE. Always read the company's own definition (footnote or non-GAAP reconciliation); they often adjust it.
- **FCFF** (cash to all capital providers) = CFO + interest x (1 - tax rate) - capex.
- **FCFE** (cash to equity) = CFO - capex + net borrowing (new debt - repayments).
- **EBITDA** = Net income + taxes + interest + D&A. Equivalently: Revenue - cash operating costs. Any other item between EBITDA and net income (restructuring, impairments, "other expense (income)") must also be added back to reconcile. "Adjusted EBITDA" is a management definition.
- **Non-cash items** are adjusted because net income is accrual-based. Two kinds: (1) truly non-cash (D&A, stock comp, impairments, deferred tax); (2) cash exists but is classified in investing or financing (gain or loss on asset sales, gain or loss on debt extinguishment).
- **Working capital rule**: an operating asset increasing uses cash (-); an operating liability increasing provides cash (+). Cash, debt and equity are not operating items.
- One-sentence version: CFO is net income with the accounting stripped out and the timing of customer and supplier payments added back in.

### FCF quick reference: formula and the two ways to get there
- **Standard FCF = CFO - capex.** Interest is already deducted: under US GAAP, cash interest paid sits inside CFO. Under IFRS, interest paid may be classified in operating or financing, so check where the company put it.
- **Way 1 (top-down):** CFO - capex. CFO is a line on the cash flow statement.
- **Way 2 (build-up):** EBITDA - cash interest - cash taxes - change in net working capital - capex. Or: Net income + D&A and other non-cash items - change in NWC - capex. Both ways must reconcile to the same number; the build-up is how to project FCF or find what drove a miss (this is the EBITDA bridge in Step 4C).
- **Unlevered FCF (FCFF) = EBIT x (1 - t) + D&A - change in NWC - capex.** Before interest; cash available to all capital providers; used in DCFs.
- **Levered FCF (FCFE)** = FCFF - after-tax interest +/- net borrowing; cash available to equity after debt service. Standard CFO - capex is already a levered measure.

### FCF vs liquidity (does FCF of -$100M mean $100M less liquidity?)
- Approximately yes over the period, if there is little financing or other activity. FCF is a flow over a period, not an instantaneous change.
- Ending cash = starting cash + FCF - dividends and buybacks - finance lease principal (often outside FCF) - debt repayments + debt issuance +/- asset sales, acquisitions, FX and other items.
- Cash is not liquidity. Liquidity = usable (unrestricted, non-trapped) cash + undrawn committed facilities. Restricted or trapped cash (escrow, foreign subsidiaries) is not available. A revolver draw raises cash and lowers availability by the same amount, so liquidity is unchanged; a company with a large undrawn revolver can lose cash without losing much liquidity, but once the revolver is drawn, further burn comes straight out of liquidity.
- Limits of FCF as a proxy for change in a company: timing noise (working capital, lumpy capex, tax timing); cutting growth capex flatters FCF; one-offs such as asset sales fall in investing and are missed; it is a lagging signal; it says nothing about maturity walls. For credit work, pair FCF with net leverage, interest coverage, liquidity versus the 12 to 24 month maturity profile, cost of new versus old debt, market signals (bond prices, CDS) and operating trends.

### Why the rules work this way (read this instead of memorizing)
- **Net income measures the wealth created for the current owners (common shareholders)** after every other claim on the business is paid: suppliers, employees (including stock comp, which costs current owners through dilution), lenders (interest), and the government (taxes). Preferred dividends are deducted to get "net income attributable to common."
- **Only items that change owners' wealth reach net income.** A transaction that swaps one form of value for another changes nothing: buying a $100 machine with cash (cash down, equipment up), borrowing (cash up, liability up), repaying principal (cash down, liability down), and issuing stock. These are balance sheet only. The machine's cost reaches net income gradually as depreciation, because the business consumes it over its life.
- **Costs of earning the profit are expenses:** wages, rent, materials, and interest (the price of using borrowed money this period). Principal is not an expense because it returns the loan; interest is.
- **Dividends and buybacks are owners taking wealth out**, not a cost of creating it, so they never reach net income.
- **The three cash flow sections mirror this:** Operating = cash from the day-to-day activities that produce net income. Investing = swapping cash for long-term assets (or back). Financing = raising or returning money to lenders and owners.
- **Why CFO needs adjustments:** net income is accrual-based (revenue when earned, expense when incurred), so it differs from cash for three reasons: (1) non-cash items (D&A, impairments, stock comp, unrealized gains); (2) timing (working capital: receivables, inventory, payables); (3) items whose cash belongs in investing or financing (gains on asset sales or debt extinguishment).
- **Interest paid is operating under US GAAP** by rule (consistency with net income), although it is arguably a financing cost. IFRS (IAS 7) lets companies classify interest paid as operating or financing, so CFO is not directly comparable across US GAAP and IFRS filers; adjust by moving interest paid in or out.
- **Test for any item:** does this change the owners' wealth, or is it just swapping one form of value for another?
- **Different "accounting" for different audiences:** financial accounting (GAAP/IFRS, audited, for outsiders), managerial (internal, no required rules), tax (follows the tax code; creates deferred taxes), regulatory (banks, insurers). Company "adjusted" measures and segment profit are management measures, not defined by GAAP; always reconcile to GAAP.

### Algorithm

**Step 1. Gather the files.** List `companies\{ticker}\`. Identify each period covered (10-Q, 10-K, 8-K Ex. 99.1). If a quarter has only an earnings release and no 10-Q yet, use the release and flag the numbers as unaudited and subject to change. Note units (usually $ millions) and fiscal year-end.

**Step 2. Extract from the Condensed Consolidated Statement of Cash Flows.** Cash flow statements are cumulative (YTD), not quarterly:
- Q1 = three months column in the Q1 10-Q
- Q2 = six months YTD (Q2 10-Q) - Q1
- Q3 = nine months YTD (Q3 10-Q) - six months YTD
- Q4 = full year (10-K) - nine months YTD

Pull every line of the operating section, plus capex ("Cost of additions to land, buildings, equipment and software" or similar), proceeds from debt, repayments of debt, dividends, and any financing line that is operating in substance (see flags). Record the page or table each number came from.

**Step 3. Compute standalone-quarter figures** by the subtraction above for every line item, not just the subtotal. If the current filing's prior-period column differs from the number in the earlier filing, the company restated; use the latest filing's figures and flag the restatement.

**Step 4. Compute CFO three ways and reconcile.**
- **A. Reported:** the "Net cash provided by (used in) operating activities" line, quarterly via Step 3.
- **B. Rebuilt (indirect):** Net income + non-cash expenses - non-cash gains +/- changes in working capital, using the statement's own lines. It must equal A; if it does not, flag and find the difference.
- **C. EBITDA bridge:** EBITDA (built from the income statement, bottom-up AND top-down; both must match) - cash interest - cash taxes (use the cash flow supplemental disclosure; fall back to expense and flag) +/- working capital - cash restructuring - pension contributions in excess of expense - other = CFO. Show the unexplained remainder as its own line rather than forcing it to zero.

**Step 5. Compute FCF.**
- FCF = CFO - capex, quarterly and YTD.
- Company-defined FCF: quote the company's definition and reconcile to CFO - capex. Show every adjustment separately.
- Optional: FCFF and FCFE using the formulas above.

**Step 6. Credit context (always include).** Cash, total debt (short and long term), debt maturity schedule by year, FCF / total debt, CFO / total debt, and any liquidity facility (revolver / ABL) with its maturity. Compare near-term maturities to FCF plus cash.

**Step 7. Write `{ticker}_fcf.md`** using this template:

```
# {TICKER} Cash Flow: CFO and FCF
Source files: ... | Units: ... | Last updated: ...

## Quarterly CFO and FCF
| Metric | Q1 | Q2 | Q3 | Q4 | FY |
| CFO (reported) |
| Capex |
| FCF (CFO - capex) |
| Company-defined FCF |

## CFO reconciliation (per quarter)
A. Reported / B. Rebuilt / C. EBITDA bridge, with residual

## Working capital detail
(each line, with sign)

## Credit context
Cash, debt, maturities, liquidity, FCF/debt

## Flags
(see below)
```

### Flags to check every time
- **YTD vs quarterly:** never report a YTD cash flow figure as a quarter.
- **Items that are economically operating but classified elsewhere (or vice versa):** e.g. Xerox's tariff receivable sale ($80M) is financing under GAAP but counted in the company's CFO and FCF guidance. Report GAAP CFO first, then the adjusted figure with the difference shown.
- **Finance receivables / lease books:** lessors run these through CFO. A shrinking book releases cash and flatters CFO; record the size of this effect.
- **Working capital reversals:** stretched payables, faster collections and receivable sales pull cash forward and tend to reverse.
- **Adjusted vs actual cash costs:** adjusted operating income and adjusted EBITDA typically exclude interest, restructuring and pension cash. Compare to CFO over several periods; a persistent gap is the signal, not one quarter.
- **Interest classification:** US GAAP puts interest paid in CFO; IFRS may not. Some companies split financing-business interest (inside operating costs) from corporate interest (separate line); note which one is deducted in each metric.
- **Stock comp** is added back in CFO but is a real cost; show FCF after SBC if material.
- **Earnings release vs 10-Q:** the release comes first, the 10-Q a few days to ~2 weeks later; reconcile when the 10-Q arrives.

---

## Skill 2: Covenant review (what can trigger a default or bankruptcy)

### Maintenance vs incurrence covenants (why it matters for timing a default)
- **Maintenance covenant:** a financial test the borrower must PASS on a schedule (usually every quarter, on a trailing four-quarter basis), whether or not it does anything. Examples: maximum total or first-lien leverage, minimum interest or fixed charge coverage, minimum liquidity, minimum revenue or asset coverage. A breach is a default (after any cure period) and can let lenders accelerate the debt, even though no payment was missed. This is how a company can be pushed toward a filing or forced into a waiver or amendment negotiation (the lenders get leverage to reprice, take collateral, or demand a restructuring). Typical home: bank loans (revolvers, term loan A, some term loan B, ABLs).
- **Incurrence covenant:** a test applied ONLY when the borrower takes a specific action: incurring debt, paying a dividend or restricted payment, making an investment, granting a lien, selling assets. If the ratio is bad, the action is blocked (or must use a basket); being over the ratio is not a default. Typical home: high yield bond indentures, and most "covenant-lite" term loan B facilities.
- **Springing maintenance covenant:** a maintenance test that is only tested when a trigger occurs (e.g., an ABL's fixed charge coverage ratio only applies once excess availability drops below a threshold; term loans sometimes have a revolver-drawn trigger). It looks harmless until liquidity tightens, then becomes the default trigger.
- **Not every default needs a ratio:** also check payment defaults (missed interest after the grace period, often 30 days for interest on bonds, none or a few days for term loans), cross-default and cross-acceleration to other debt, judgment defaults, change of control, and non-financial covenants (information, collateral). In structured or IP/royalty structures, a minimum revenue, asset coverage or royalty test may work as a maintenance covenant even though it is not labeled "financial covenant".
- **Equity cures:** many agreements let the sponsor or company inject equity (a "cure right") to fix a maintenance breach; note the limits (number of cures, amount).
- **Remember the incurrence ratios still matter** for capacity: they decide how much additional secured debt, unsecured debt, dividends and investments are allowed, which tells you what can be primed or layered ahead of existing creditors.

### Algorithm for each credit document in `companies\{ticker}\filings\`
1. Identify the document (credit agreement, indenture, amendment) from `filings_manifest.md`. Always prefer the most recent amendment or conformed copy; amendments change thresholds (e.g., ABL springing trigger raised from $22.5M to $31.875M after the commitments increased).
2. Find the financial covenant section: search for "Financial Covenant", "Covenant Compliance", "shall not permit", "Fixed Charge Coverage", "Leverage Ratio", "Interest Coverage", "Minimum Liquidity", "Minimum", "Asset Coverage". Check the table of contents of the negative covenants article for "[Reserved]" placeholders: a reserved financial covenant section often means the facility has none.
3. Classify each ratio as maintenance, springing maintenance, or incurrence; record: the test (formula and definitions of EBITDA and debt used), threshold (and step-downs), test frequency, trigger, cure rights, consequence of breach, and the cross-default linkage.
4. Compute where the company stands today on each maintenance test using the latest financials (use the company's definitions, which add back items; note the definitional EBITDA is usually higher than reported EBITDA) and the headroom.
5. Record in `{ticker}_credit_terms.md` and flag any covenant that is close to breach, any step-down coming up, and any document whose maintenance test is NOT present.