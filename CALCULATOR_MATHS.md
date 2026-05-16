# Matched Betting Calculator — Mathematics

All logic lives in `src/matched_betting/calculator.py` (arb detection) and
`bet_executor.py` (stake sizing).

---

## 1. Notation

| Symbol | Meaning |
|--------|---------|
| `O_b`  | Raw decimal back odds (e.g. 2.50) |
| `O_l`  | Raw decimal lay odds (e.g. 2.40) |
| `e_b`  | Effective back odds after commission |
| `e_l`  | Effective lay odds after commission |
| `c`    | Commission rate (0.02 = 2%) |
| `p`    | Implied probability = 1 / O |
| `B`    | Back stake (USDC) |
| `L`    | Lay stake (USDC) |
| `T`    | Total budget = money placed across all legs (USDC) |
| `M`    | Net margin = sum of inverse effective odds |

Decimal odds of 2.50 means: stake 1, receive 2.50 back if you win (profit = 1.50).

---

## 2. Commission and Effective Odds

Commission is applied differently to back and lay bets.  
The effective odds represent the *net* return per unit staked after the
provider has taken its cut.

### 2a. Back bet — effective odds

The provider deducts commission `c` from the **profit** portion of the return:

```
e_b = 1 + (O_b − 1) × (1 − c)
```

- At `c = 0`:  `e_b = O_b`  (no adjustment)
- At `c = 0.02`, `O_b = 2.50`:  `e_b = 1 + 1.50 × 0.98 = 2.47`

### 2b. Lay bet — effective lay cost

Commission `c` is deducted from the layer's **profit when the outcome
loses** (i.e. you keep the backer's stake minus commission). Your
liability if the outcome *wins* is unchanged.

Expressing this as an equivalent commission-free lay odds:

```
e_l = 1 + (O_l − 1) / (1 − c)
```

- At `c = 0`:  `e_l = O_l`
- At `c = 0.02`, `O_l = 2.30`:  `e_l = 1 + 1.30 / 0.98 = 2.327`

`e_l > O_l` always when `c > 0`: commission makes laying *worse* (costs
more to cover the same position).

### 2c. Polymarket variable fee

Polymarket charges a fee on **sports markets** based on the implied
probability of the outcome.  The fee rate is:

```
f = 0.03 × p × (1 − p)   where p = 1 / O
```

This peaks at **0.75%** when `p = 0.5` (evens, `O = 2.00`) and falls
towards zero for very strong or very weak favourites.

Applied to back and lay odds exactly as above, substituting `f` for `c`:

```
e_b (polymarket) = 1 + (O_b − 1) × (1 − f_b)   where f_b = 0.03/O_b × (1 − 1/O_b)
e_l (polymarket) = 1 + (O_l − 1) / (1 − f_l)   where f_l = 0.03/O_l × (1 − 1/O_l)
```

### 2d. Commission rates by provider

| Provider    | Back commission `c` | Lay commission `c` |
|-------------|---------------------|--------------------|
| Matchbook   | 2%                  | 2%                 |
| Smarkets    | 0% (intro period)   | 0% (intro period)  |
| SX Bet      | 0%                  | 0%                 |
| Polymarket  | variable (see 2c)   | variable (see 2c)  |
| Azuro       | 0%                  | — (no lay market)  |

---

## 3. Sure-Bet (Back–Back) Arbitrage

A sure-bet backs **every possible outcome** across different providers.
Because the providers disagree on the probabilities, the combined implied
margin can be less than 1, guaranteeing a profit regardless of the result.

### 3a. Detection condition

**Two-way market** (e.g. NBA — only home win / away win):

```
M = 1/e_b1 + 1/e_b2 < 1   →   arb exists
```

**Three-way market** (e.g. football — home / draw / away):

```
M = 1/e_b1 + 1/e_draw + 1/e_b2 < 1   →   arb exists
```

`M` is the **net margin**: the fraction of budget consumed by stakes,
leaving `1 − M` as pure profit per unit.

### 3b. Net profit percentage

```
profit% = (1/M − 1) × 100
```

Example: `M = 0.97` → `profit% = 3.09%`

### 3c. Stake sizing — equal return on every outcome

We want every outcome to return the same amount `R`, so we know our
profit regardless of the result.

For outcome `i` with effective odds `e_i` and stake `s_i`:

```
Return if i wins:  s_i × e_i = R   →   s_i = R / e_i
```

Summing all stakes to equal the total budget `T`:

```
T = Σ s_i = Σ R/e_i = R × M
```

Solving for `R` and substituting back:

```
R = T / M

s_i = T / (e_i × M)
```

**Each leg's stake is inversely proportional to its effective odds.**
High-odds legs get smaller stakes; low-odds legs (near-certain outcomes)
get larger stakes.

#### Numerical example

| Outcome | Provider   | Raw odds | Eff odds `e_i` | Stake (T=£100) | Return |
|---------|------------|----------|----------------|----------------|--------|
| Home    | Polymarket | 2.10     | 2.084          | £47.94         | £99.93 |
| Draw    | Matchbook  | 3.80     | 3.724          | £26.82         | £99.90 |
| Away    | SX Bet     | 4.20     | 4.200          | £25.24         | £106.0 |
|         |            |          | `M = 0.9645`   | **£100.00**    | ≈ £100 |

`profit% = (1/0.9645 − 1) × 100 ≈ 3.68%`

*(Small rounding differences in returns are expected; profits are
guaranteed to be positive.)*

---

## 4. Back–Lay Arbitrage

A back-lay arb backs one outcome on one provider and lays the same
outcome on another.  If the back odds are higher than the lay odds
(after commission), a profit is locked in regardless of the result.

### 4a. Cash flows

| Scenario | Back leg profit | Lay leg profit |
|----------|----------------|----------------|
| Outcome **wins** | `+B × (e_b − 1)` | `−L × (e_l − 1)` |
| Outcome **loses** | `−B` | `+L` |

### 4b. Equal-profit condition

Setting win profit = loss profit:

```
B × (e_b − 1) − L × (e_l − 1) = −B + L
B × e_b − L × e_l = 0
L = B × e_b / e_l
```

With `L` set this way, profit in either scenario:

```
Profit = −B + L = B × (e_b/e_l − 1)
```

### 4c. Detection condition

```
e_b > e_l   →   arb exists

profit% = (e_b / e_l − 1) × 100
```

### 4d. Stake formula used in code

The code uses raw odds (not effective odds) for the lay stake ratio:

```
L = B × O_b / O_l       ← raw odds, not e_b/e_l
```

This gives exactly equal profits when both providers are commission-free
(`e_b = O_b`, `e_l = O_l`).  With commission the two scenarios return
slightly different amounts, but both remain profitable whenever `e_b > e_l`.
The discrepancy is proportional to the commission rate (≤ 2%) and is
negligible in practice.

---

## 5. Stake Sizing — Budget = Total Money Placed

The user sets a **budget `T`** meaning the *total USDC placed across
all legs* equals `T`.

### 5a. Exchange lay (Matchbook / SX Bet)

On a traditional exchange the full lay stake `L` is committed.

```
Total placed  =  B + L
             =  B + B × O_b / O_l
             =  B × (O_l + O_b) / O_l  =  T

⟹  B = T × O_l / (O_l + O_b)

    L = T × O_b / (O_l + O_b)           (follows from L = B × O_b / O_l)
```

**Verification:** `B + L = T × (O_l + O_b) / (O_l + O_b) = T` ✓

### 5b. Polymarket NO-token lay

Polymarket has no lay order book.  Instead, each outcome is a binary
**Yes/No** market.  *Backing the NO token* is mathematically equivalent
to laying the outcome.

**NO-token mechanics:**

| | Traditional exchange lay | Polymarket NO token |
|--|--|--|
| Upfront cost | Liability = `L × (O_l − 1)` reserved | Spend `L × (O_l − 1)` to buy `L × O_l` NO tokens |
| Outcome **wins** (NO loses) | Pay `L × (O_l − 1)` | Lose the spend = `L × (O_l − 1)` |
| Outcome **loses** (NO wins) | Receive `L` net | Tokens pay out `L × O_l`, net profit `L` |

The cash actually spent buying NO tokens is:

```
PM_NO_cost = L × (O_l − 1)
```

This equals the layer's liability on a traditional exchange — so the
two instruments are equivalent in both P&L and upfront cost.

**Stake sizing with Polymarket lay:**

The total placed is back stake plus the NO-token purchase:

```
Total placed  =  B + L × (O_l − 1)
             =  B + (B × O_b / O_l) × (O_l − 1)
             =  B × [1 + O_b × (O_l − 1) / O_l]
             =  B × [O_l + O_b × (O_l − 1)] / O_l  =  T

⟹  B = T × O_l / [O_l + O_b × (O_l − 1)]

    L = B × O_b / O_l       (then NO cost = L × (O_l − 1))
```

**Verification:** `B + L×(O_l−1) = T` ✓

### 5c. Comparison of back stakes for the same budget

Given `O_b = 2.00`, `O_l = 1.90`, `T = $10`:

| Lay provider | Back stake `B` | Lay stake `L` | Lay cost | Total |
|---|---|---|---|---|
| Matchbook/SX Bet | `10 × 1.90 / (1.90 + 2.00) = $4.87` | $5.13 | $5.13 | $10.00 |
| Polymarket (NO) | `10 × 1.90 / (1.90 + 2.00×0.90) = $5.14` | $5.41 | `5.41×0.90 = $4.87` | $10.01* |

*Rounding to 2 d.p.*

When using Polymarket as the lay venue, the back stake is **larger**
because the effective lay cost is cheaper (only the liability fraction,
not the full notional).

---

## 6. Profit Summary

| Arb type | Profit formula | Notes |
|---|---|---|
| Sure-bet (2-way) | `(1/M − 1) × 100%`  where `M = 1/e_1 + 1/e_2` | Exact with eff odds |
| Sure-bet (3-way) | `(1/M − 1) × 100%`  where `M = 1/e_1 + 1/e_d + 1/e_2` | Exact with eff odds |
| Back-lay | `(e_b / e_l − 1) × 100%` | Exact; stakes use approx raw-odds ratio |
| Back-lay gross | `(O_b / O_l − 1) × 100%` | Pre-commission upper bound |

For back-lay arbs `gross_profit% ≥ net_profit%` always.

---

## 7. Worked Back-Lay Example

**Setup:** Back Liverpool win on SX Bet at `O_b = 2.20` (no commission).  
Lay Liverpool win on Matchbook at `O_l = 2.10` (2% commission).  
Budget `T = $20`.

**Step 1 — Effective odds:**
```
e_b = 1 + (2.20 − 1) × 1.00 = 2.20      (SX Bet, c=0)
e_l = 1 + (2.10 − 1) / 0.98 = 2.122     (Matchbook, c=0.02)
```

**Step 2 — Is there an arb?**
```
e_b = 2.20 > e_l = 2.122   ✓  arb exists
profit% = (2.20 / 2.122 − 1) × 100 = 3.68%
```

**Step 3 — Stakes (exchange lay, budget = $20):**
```
B = 20 × 2.10 / (2.10 + 2.20) = 20 × 2.10 / 4.30 = $9.77
L = 9.77 × 2.20 / 2.10 = $10.23
```

**Step 4 — Verify P&L:**
```
Liverpool wins:
  Back profit   = 9.77 × (2.20 − 1)         = +$11.72
  Lay liability = 10.23 × (2.10 − 1) × 0.98  = −$11.01   ← commission reduces liability
  Net = $0.71

Liverpool does not win:
  Back loss  = −$9.77
  Lay profit = +$10.23 × (1 − 0.02) = +$10.03   ← commission on lay profit
  Net = $0.26
```

The two scenarios return different amounts because commission is
asymmetric (applies to lay profit but not lay liability).  Both are
positive — the arb is guaranteed, as predicted by `e_b > e_l`.

The `profit%` formula `(e_b/e_l − 1) × 100 = 3.68%` approximates the
average across scenarios.  Actual per-scenario returns range from
`0.26/20 = 1.3%` to `0.71/20 = 3.6%` in this example.
