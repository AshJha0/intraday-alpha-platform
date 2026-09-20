//! `DecisionTrace` (`schemas/trace/decision_trace.schema.json`) and every
//! nested stage record, the JSONL trace sink, the stream digest and the
//! pinned `explain()` rendering (`iap.contracts.types` / `iap.trace`).
//!
//! Every record is a strict serde struct (`deny_unknown_fields`, every key
//! required — a `null` stage is written explicitly, never omitted) whose wire
//! enums carry the schema's encodings: integers for `direction` (-1/0/1),
//! `decision` (1..3), `side` (0/1), `order_type` (1..6) and `status` (1..6);
//! names for `solver_status` and `algo`.
//!
//! A trace is persisted as ONE canonical-JSON line ([`DecisionTrace::to_canonical_line`]);
//! [`TraceDigest`] hashes `line + "\n"` per trace so the digest of a run equals
//! the digest of its JSONL file re-canonicalised (`store_trace.md` §4).

use std::collections::BTreeMap;
use std::io::Write;

use marketdata::IapError;
use serde::de::Deserializer;
use serde::{Deserialize, Serialize};
use serde_json::Value;

use crate::canonical::{canonical_json, is_generic_id, is_sha256_hex, is_trace_id};
use crate::sha256::Sha256;

// ----------------------------------------------------------------- enums ---

macro_rules! int_enum {
    ($(#[$doc:meta])* $name:ident : $($variant:ident = $code:literal),+ $(,)?) => {
        $(#[$doc])*
        #[derive(Debug, Clone, Copy, PartialEq, Eq, Hash, Serialize, Deserialize)]
        #[serde(try_from = "i64", into = "i64")]
        pub enum $name {
            $(
                #[doc = concat!("Wire value ", stringify!($code), ".")]
                $variant,
            )+
        }

        impl From<$name> for i64 {
            fn from(v: $name) -> i64 {
                match v {
                    $($name::$variant => $code,)+
                }
            }
        }

        impl TryFrom<i64> for $name {
            type Error = String;

            fn try_from(code: i64) -> Result<$name, String> {
                match code {
                    $($code => Ok($name::$variant),)+
                    other => Err(format!(concat!(stringify!($name), ": unknown wire value {}"), other)),
                }
            }
        }

        impl $name {
            /// The wire integer.
            pub fn code(self) -> i64 {
                i64::from(self)
            }
        }
    };
}

int_enum! {
    /// Sign of an alpha signal (`alpha_signal.schema.json`).
    Direction: Down = -1, Flat = 0, Up = 1
}

int_enum! {
    /// Hard-risk decision (`risk_decision.schema.json`).
    Decision: Allow = 1, Reject = 2, Kill = 3
}

int_enum! {
    /// Order side (conventions §1).
    Side: Bid = 0, Ask = 1
}

int_enum! {
    /// Child order type (`child_order.schema.json`).
    OrderType: Market = 1, Limit = 2, Ioc = 3, Fok = 4, Peg = 5, Mid = 6
}

int_enum! {
    /// Execution report status (`execution_report.schema.json`).
    ExecStatus: New = 1, Partial = 2, Filled = 3, Canceled = 4, Rejected = 5, Expired = 6
}

impl Decision {
    /// Schema name (`ALLOW` / `REJECT` / `KILL`), as `explain()` prints it.
    pub fn name(self) -> &'static str {
        match self {
            Decision::Allow => "ALLOW",
            Decision::Reject => "REJECT",
            Decision::Kill => "KILL",
        }
    }
}

/// Portfolio solve outcome (`portfolio_target.schema.json`).
#[derive(Debug, Clone, Copy, PartialEq, Eq, Hash, Serialize, Deserialize)]
pub enum SolverStatus {
    /// Converged.
    #[serde(rename = "OPTIMAL")]
    Optimal,
    /// Constraints cannot be met.
    #[serde(rename = "INFEASIBLE")]
    Infeasible,
    /// Iteration budget exhausted.
    #[serde(rename = "MAX_ITER")]
    MaxIter,
}

impl SolverStatus {
    /// Wire name.
    pub fn name(self) -> &'static str {
        match self {
            SolverStatus::Optimal => "OPTIMAL",
            SolverStatus::Infeasible => "INFEASIBLE",
            SolverStatus::MaxIter => "MAX_ITER",
        }
    }
}

/// Parent-order execution algorithm (`parent_order.schema.json`).
#[derive(Debug, Clone, Copy, PartialEq, Eq, Hash, Serialize, Deserialize)]
pub enum Algo {
    /// Time-weighted.
    #[serde(rename = "TWAP")]
    Twap,
    /// Volume-weighted.
    #[serde(rename = "VWAP")]
    Vwap,
    /// Percentage of volume (`params.participation`).
    #[serde(rename = "POV")]
    Pov,
    /// Implementation shortfall.
    #[serde(rename = "IS")]
    Is,
}

impl Algo {
    /// Wire name.
    pub fn name(self) -> &'static str {
        match self {
            Algo::Twap => "TWAP",
            Algo::Vwap => "VWAP",
            Algo::Pov => "POV",
            Algo::Is => "IS",
        }
    }
}

// --------------------------------------------------------------- records ---

/// A required `Option` field: the key must be present (`null` allowed),
/// unlike serde's default of treating a missing `Option` as `None`.
fn required_option<'de, D, T>(d: D) -> Result<Option<T>, D::Error>
where
    D: Deserializer<'de>,
    T: Deserialize<'de>,
{
    Option::<T>::deserialize(d)
}

/// One alpha signal (`alpha/alpha_signal.schema.json`).
#[derive(Debug, Clone, PartialEq, Serialize, Deserialize)]
#[serde(deny_unknown_fields)]
pub struct AlphaSignal {
    /// Event time, ns.
    pub timestamp: i64,
    /// Instrument.
    pub instrument_id: u32,
    /// Dimensionless expected return (1e-4 = 1 bp).
    pub expected_return: f64,
    /// Confidence in `[0, 1]`.
    pub confidence: f64,
    /// Horizon, ns.
    pub horizon_ns: i64,
    /// Sign.
    pub direction: Direction,
    /// Model / parameter-set label.
    pub model_version: String,
}

/// One target leg (`portfolio_target#/$defs/PortfolioLeg`).
#[derive(Debug, Clone, PartialEq, Serialize, Deserialize)]
#[serde(deny_unknown_fields)]
pub struct PortfolioLeg {
    /// Instrument.
    pub instrument_id: u32,
    /// Target position.
    pub target_qty: i64,
    /// Target weight.
    pub target_weight: f64,
    /// Expected return, bps.
    pub expected_return_bps: f64,
    /// Position before the solve.
    pub prev_qty: i64,
}

/// Portfolio construction output (`portfolio/portfolio_target.schema.json`).
#[derive(Debug, Clone, PartialEq, Serialize, Deserialize)]
#[serde(deny_unknown_fields)]
pub struct PortfolioTarget {
    /// Strategy.
    pub strategy_id: String,
    /// Event time, ns.
    pub timestamp_ns: i64,
    /// Hash of constraints + solver params.
    pub portfolio_version: String,
    /// Feature-registry hash.
    pub feature_version: String,
    /// Model hash.
    pub model_version: String,
    /// Solve outcome.
    pub solver_status: SolverStatus,
    /// Objective at the solution.
    pub objective_value: f64,
    /// Turnover (>= 0).
    pub turnover: f64,
    /// Legs sorted by unique instrument id.
    pub targets: Vec<PortfolioLeg>,
}

/// Hard-risk decision (`risk/risk_decision.schema.json`).
#[derive(Debug, Clone, PartialEq, Serialize, Deserialize)]
#[serde(deny_unknown_fields)]
pub struct RiskDecision {
    /// Order.
    pub order_id: u64,
    /// Strategy.
    pub strategy_id: String,
    /// Instrument.
    pub instrument_id: u32,
    /// Event time, ns.
    pub timestamp_ns: i64,
    /// ALLOW / REJECT / KILL.
    pub decision: Decision,
    /// Deciding rule id ("" for ALLOW).
    pub rule_id: String,
    /// Pinned check index (-1 for ALLOW).
    pub rule_index: i64,
    /// Reason text.
    pub reason: String,
}

/// Parent order (`order/parent_order.schema.json`).
#[derive(Debug, Clone, PartialEq, Serialize, Deserialize)]
#[serde(deny_unknown_fields)]
pub struct ParentOrder {
    /// Parent order id.
    pub parent_order_id: u64,
    /// Strategy.
    pub strategy_id: String,
    /// Alpha that produced it.
    pub alpha_id: String,
    /// Instrument.
    pub instrument_id: u32,
    /// Side.
    pub side: Side,
    /// Quantity (>= 1).
    pub qty: i64,
    /// Algorithm.
    pub algo: Algo,
    /// Decision time, ns.
    pub decision_ts: i64,
    /// Arrival time, ns.
    pub arrival_ts: i64,
    /// End time, ns.
    pub end_ts: i64,
    /// Urgency in `[0, 1]`.
    pub urgency: f64,
    /// Limit price (0 = unpriced).
    pub limit_price_ticks: i64,
    /// Algorithm parameters (`participation` for POV).
    pub params: BTreeMap<String, f64>,
}

/// Child order (`order/child_order.schema.json`).
#[derive(Debug, Clone, PartialEq, Serialize, Deserialize)]
#[serde(deny_unknown_fields)]
pub struct ChildOrder {
    /// Child order id.
    pub child_order_id: u64,
    /// Parent.
    pub parent_order_id: u64,
    /// Instrument.
    pub instrument_id: u32,
    /// Venue (0 = SOR decides).
    pub venue_id: u16,
    /// Side.
    pub side: Side,
    /// Quantity (>= 1).
    pub qty: i64,
    /// Price (0 for MARKET).
    pub price_ticks: i64,
    /// Order type.
    pub order_type: OrderType,
    /// Submit time, ns.
    pub submit_ts: i64,
    /// Expiry (0 = parent end).
    pub expire_ts: i64,
    /// Slice index.
    pub slice_index: u32,
}

/// One scored venue (`venue_decision#/$defs/VenueScore`).
#[derive(Debug, Clone, PartialEq, Serialize, Deserialize)]
#[serde(deny_unknown_fields)]
pub struct VenueScore {
    /// Venue.
    pub venue_id: u16,
    /// Eligible for routing.
    pub eligible: bool,
    /// Displayed price.
    pub displayed_price_ticks: i64,
    /// Displayed size.
    pub displayed_qty: i64,
    /// Taker fee.
    pub taker_fee: f64,
    /// Maker rebate.
    pub maker_rebate: f64,
    /// Commission per million.
    pub commission_per_million: f64,
    /// Mean latency, ns.
    pub latency_mean_ns: i64,
    /// Rank (>= 1 iff eligible).
    pub rank: u16,
}

/// SOR decision (`execution/venue_decision.schema.json`).
#[derive(Debug, Clone, PartialEq, Serialize, Deserialize)]
#[serde(deny_unknown_fields)]
pub struct VenueDecision {
    /// Child order routed.
    pub child_order_id: u64,
    /// Chosen venue (0 = NO_ROUTE).
    pub venue_id: u16,
    /// Reason text.
    pub reason: String,
    /// Candidates sorted by unique venue id.
    pub candidates: Vec<VenueScore>,
}

/// Execution report (`execution/execution_report.schema.json`).
#[derive(Debug, Clone, PartialEq, Serialize, Deserialize)]
#[serde(deny_unknown_fields)]
pub struct ExecutionReport {
    /// Order (child) id.
    pub order_id: u64,
    /// Execution id.
    pub execution_id: u64,
    /// Status.
    pub status: ExecStatus,
    /// This report's fill quantity.
    pub filled_qty: i64,
    /// Fill price.
    pub fill_price_ticks: i64,
    /// Venue.
    pub venue_id: u16,
    /// Venue time, ns.
    pub exchange_ts: i64,
    /// Receive time, ns.
    pub receive_ts: i64,
    /// Fees (negative = rebate).
    pub fees: f64,
}

/// Latency summary (`tca_result#/$defs/LatencyStats`), ns.
#[derive(Debug, Clone, PartialEq, Serialize, Deserialize)]
#[serde(deny_unknown_fields)]
pub struct LatencyStats {
    /// Minimum.
    pub min: i64,
    /// Mean.
    pub mean: f64,
    /// Maximum.
    pub max: i64,
    /// Median (nearest rank).
    pub p50: i64,
    /// 99th percentile (nearest rank).
    pub p99: i64,
}

/// TCA result (`tca/tca_result.schema.json`).
#[derive(Debug, Clone, PartialEq, Serialize, Deserialize)]
#[serde(deny_unknown_fields)]
pub struct TcaResult {
    /// Parent order.
    pub parent_order_id: u64,
    /// Instrument.
    pub instrument_id: u32,
    /// Side.
    pub side: Side,
    /// Ordered quantity.
    pub qty: i64,
    /// Filled quantity.
    pub filled_qty: i64,
    /// filled / qty.
    pub fill_rate: f64,
    /// Arrival price.
    pub arrival_price_ticks: i64,
    /// Average fill price, ticks.
    pub avg_fill_price: f64,
    /// Interval VWAP, ticks.
    pub interval_vwap: f64,
    /// Interval TWAP, ticks.
    pub interval_twap: f64,
    /// IS = delay + trading + opportunity.
    pub implementation_shortfall_bps: f64,
    /// Delay cost.
    pub delay_cost_bps: f64,
    /// Trading cost = spread + impact + timing.
    pub trading_cost_bps: f64,
    /// Opportunity cost.
    pub opportunity_cost_bps: f64,
    /// Spread cost.
    pub spread_cost_bps: f64,
    /// Impact.
    pub impact_bps: f64,
    /// Fees.
    pub fees_bps: f64,
    /// Timing cost.
    pub timing_cost_bps: f64,
    /// Arrival slippage.
    pub slippage_bps: f64,
    /// Participation rate.
    pub participation_rate: f64,
    /// Number of fills.
    pub n_fills: u32,
    /// Contribution per venue (decimal venue id -> bps).
    pub venue_contribution_bps: BTreeMap<String, f64>,
    /// Algorithm.
    pub algo: Algo,
    /// Latency summary.
    pub latency_ns: LatencyStats,
}

/// P&L attribution (`decision_trace#/$defs/Attribution`), bps.
#[derive(Debug, Clone, PartialEq, Serialize, Deserialize)]
#[serde(deny_unknown_fields)]
pub struct Attribution {
    /// Alpha contribution.
    pub alpha_bps: f64,
    /// Spread (negative = cost).
    pub spread_bps: f64,
    /// Impact.
    pub impact_bps: f64,
    /// Fees.
    pub fees_bps: f64,
    /// Timing.
    pub timing_bps: f64,
    /// Sum of the five.
    pub total_bps: f64,
}

/// Stage outputs (`decision_trace#/$defs/TraceStages`); empty / `None` =
/// the stage did not run.
#[derive(Debug, Clone, PartialEq, Serialize, Deserialize)]
#[serde(deny_unknown_fields)]
pub struct TraceStages {
    /// Alpha signals.
    pub signal: Vec<AlphaSignal>,
    /// Portfolio target.
    #[serde(deserialize_with = "required_option")]
    pub portfolio: Option<PortfolioTarget>,
    /// Risk decisions.
    pub risk: Vec<RiskDecision>,
    /// Parent orders.
    pub parent_orders: Vec<ParentOrder>,
    /// Child orders.
    pub child_orders: Vec<ChildOrder>,
    /// Routing decisions.
    pub routing: Vec<VenueDecision>,
    /// Fills.
    pub fills: Vec<ExecutionReport>,
    /// TCA results.
    pub tca: Vec<TcaResult>,
    /// Attribution.
    #[serde(deserialize_with = "required_option")]
    pub attribution: Option<Attribution>,
}

impl TraceStages {
    /// Every stage absent.
    pub fn empty() -> TraceStages {
        TraceStages {
            signal: Vec::new(),
            portfolio: None,
            risk: Vec::new(),
            parent_orders: Vec::new(),
            child_orders: Vec::new(),
            routing: Vec::new(),
            fills: Vec::new(),
            tca: Vec::new(),
            attribution: None,
        }
    }
}

/// The auditable chain for one decision (`trace/decision_trace.schema.json`).
#[derive(Debug, Clone, PartialEq, Serialize, Deserialize)]
#[serde(deny_unknown_fields)]
pub struct DecisionTrace {
    /// 32-hex id (`make_trace_id`).
    pub trace_id: String,
    /// Session.
    pub session_id: String,
    /// Instrument.
    pub instrument_id: u32,
    /// Triggering event time, ns.
    pub event_ts: i64,
    /// Triggering event sequence.
    pub sequence: u64,
    /// Dataset hash.
    pub data_version: String,
    /// Feature-registry hash.
    pub feature_version: String,
    /// Model hash.
    pub model_version: String,
    /// Configuration hash.
    pub config_version: String,
    /// Stage outputs.
    pub stages: TraceStages,
}

fn finite(name: &str, x: f64) -> Result<(), IapError> {
    if x.is_finite() {
        Ok(())
    } else {
        Err(IapError::Validation(format!(
            "{name}: non-finite float {x}"
        )))
    }
}

fn sha_field(name: &str, s: &str) -> Result<(), IapError> {
    if is_sha256_hex(s) {
        Ok(())
    } else {
        Err(IapError::Validation(format!(
            "{name}: expected a lowercase sha256 hex, got {s:?}"
        )))
    }
}

fn id_field(name: &str, s: &str) -> Result<(), IapError> {
    if is_generic_id(s) {
        Ok(())
    } else {
        Err(IapError::Validation(format!(
            "{name}: {s:?} is not a valid identifier"
        )))
    }
}

impl DecisionTrace {
    /// Check the wire domains a serde parse cannot express: id / hash
    /// patterns, finite floats everywhere, `confidence == 0 ⇒ expected_return
    /// == 0`, `ALLOW ⇔ rule_index == -1`, and `trace_id` consistent with the
    /// header (`make_trace_id`).
    pub fn validate(&self) -> Result<(), IapError> {
        if !is_trace_id(&self.trace_id) {
            return Err(IapError::Validation(format!(
                "trace_id: {:?} is not 32 hex",
                self.trace_id
            )));
        }
        id_field("session_id", &self.session_id)?;
        let expected = crate::canonical::make_trace_id(
            &self.session_id,
            self.instrument_id,
            self.event_ts,
            self.sequence,
        );
        if expected != self.trace_id {
            return Err(IapError::Validation(format!(
                "trace_id {} does not match header (expected {expected})",
                self.trace_id
            )));
        }
        sha_field("data_version", &self.data_version)?;
        sha_field("feature_version", &self.feature_version)?;
        sha_field("model_version", &self.model_version)?;
        sha_field("config_version", &self.config_version)?;
        let st = &self.stages;
        for (i, s) in st.signal.iter().enumerate() {
            finite("stages.signal.expected_return", s.expected_return)?;
            finite("stages.signal.confidence", s.confidence)?;
            if !(0.0..=1.0).contains(&s.confidence) {
                return Err(IapError::Validation(format!(
                    "stages.signal[{i}].confidence {} outside [0, 1]",
                    s.confidence
                )));
            }
            if s.confidence == 0.0 && s.expected_return != 0.0 {
                return Err(IapError::Validation(format!(
                    "stages.signal[{i}]: confidence 0 requires expected_return 0"
                )));
            }
        }
        if let Some(p) = &st.portfolio {
            id_field("stages.portfolio.strategy_id", &p.strategy_id)?;
            sha_field("stages.portfolio.portfolio_version", &p.portfolio_version)?;
            sha_field("stages.portfolio.feature_version", &p.feature_version)?;
            sha_field("stages.portfolio.model_version", &p.model_version)?;
            finite("stages.portfolio.objective_value", p.objective_value)?;
            finite("stages.portfolio.turnover", p.turnover)?;
            for leg in &p.targets {
                finite("stages.portfolio.targets.target_weight", leg.target_weight)?;
                finite(
                    "stages.portfolio.targets.expected_return_bps",
                    leg.expected_return_bps,
                )?;
            }
        }
        for (i, r) in st.risk.iter().enumerate() {
            id_field("stages.risk.strategy_id", &r.strategy_id)?;
            let allow = r.decision == Decision::Allow;
            if allow != (r.rule_index == -1) {
                return Err(IapError::Validation(format!(
                    "stages.risk[{i}]: ALLOW iff rule_index == -1 (decision {}, rule_index {})",
                    r.decision.name(),
                    r.rule_index
                )));
            }
        }
        for po in &st.parent_orders {
            id_field("stages.parent_orders.strategy_id", &po.strategy_id)?;
            id_field("stages.parent_orders.alpha_id", &po.alpha_id)?;
            finite("stages.parent_orders.urgency", po.urgency)?;
            for (k, v) in &po.params {
                finite(&format!("stages.parent_orders.params.{k}"), *v)?;
            }
        }
        for vd in &st.routing {
            for c in &vd.candidates {
                finite("stages.routing.candidates.taker_fee", c.taker_fee)?;
                finite("stages.routing.candidates.maker_rebate", c.maker_rebate)?;
                finite(
                    "stages.routing.candidates.commission_per_million",
                    c.commission_per_million,
                )?;
            }
        }
        for er in &st.fills {
            finite("stages.fills.fees", er.fees)?;
        }
        for t in &st.tca {
            for (name, x) in [
                ("fill_rate", t.fill_rate),
                ("avg_fill_price", t.avg_fill_price),
                ("interval_vwap", t.interval_vwap),
                ("interval_twap", t.interval_twap),
                (
                    "implementation_shortfall_bps",
                    t.implementation_shortfall_bps,
                ),
                ("delay_cost_bps", t.delay_cost_bps),
                ("trading_cost_bps", t.trading_cost_bps),
                ("opportunity_cost_bps", t.opportunity_cost_bps),
                ("spread_cost_bps", t.spread_cost_bps),
                ("impact_bps", t.impact_bps),
                ("fees_bps", t.fees_bps),
                ("timing_cost_bps", t.timing_cost_bps),
                ("slippage_bps", t.slippage_bps),
                ("participation_rate", t.participation_rate),
                ("latency_ns.mean", t.latency_ns.mean),
            ] {
                finite(&format!("stages.tca.{name}"), x)?;
            }
            for (k, v) in &t.venue_contribution_bps {
                finite(&format!("stages.tca.venue_contribution_bps.{k}"), *v)?;
            }
        }
        if let Some(a) = &st.attribution {
            for (name, x) in [
                ("alpha_bps", a.alpha_bps),
                ("spread_bps", a.spread_bps),
                ("impact_bps", a.impact_bps),
                ("fees_bps", a.fees_bps),
                ("timing_bps", a.timing_bps),
                ("total_bps", a.total_bps),
            ] {
                finite(&format!("stages.attribution.{name}"), x)?;
            }
        }
        Ok(())
    }

    /// The JSON document (`to_dict()` in Python), after [`validate`](Self::validate).
    pub fn to_value(&self) -> Result<Value, IapError> {
        self.validate()?;
        serde_json::to_value(self).map_err(|e| IapError::Codec(format!("DecisionTrace: {e}")))
    }

    /// Strict parse of a JSON document (unknown / missing keys, wrong enum
    /// values and domain violations are errors).
    pub fn from_value(v: &Value) -> Result<DecisionTrace, IapError> {
        let trace: DecisionTrace = serde_json::from_value(v.clone())
            .map_err(|e| IapError::Codec(format!("DecisionTrace: {e}")))?;
        trace.validate()?;
        Ok(trace)
    }

    /// Parse one JSONL line.
    pub fn from_json_line(line: &str) -> Result<DecisionTrace, IapError> {
        let v: Value = serde_json::from_str(line)
            .map_err(|e| IapError::Codec(format!("DecisionTrace line: {e}")))?;
        DecisionTrace::from_value(&v)
    }

    /// The canonical JSONL line (no trailing newline) — exactly one
    /// `JsonlTraceSink` line and the unit the digest hashes.
    pub fn to_canonical_line(&self) -> Result<String, IapError> {
        canonical_json(&self.to_value()?)
    }
}

// ------------------------------------------------------------ digest/sink ---

/// Running SHA-256 over `line + "\n"` for every trace emitted, in order
/// (`iap.trace.digest.TraceDigest`). The empty stream digests to
/// `e3b0c442…b855`.
#[derive(Debug, Clone, Default)]
pub struct TraceDigest {
    hasher: Sha256,
    count: u64,
}

impl TraceDigest {
    /// Empty digest.
    pub fn new() -> TraceDigest {
        TraceDigest::default()
    }

    /// Hash one already-canonical line (without its newline).
    pub fn update_line(&mut self, line: &str) {
        self.hasher.update(line.as_bytes());
        self.hasher.update(b"\n");
        self.count += 1;
    }

    /// Canonicalise and hash one trace.
    pub fn update(&mut self, trace: &DecisionTrace) -> Result<(), IapError> {
        self.update_line(&trace.to_canonical_line()?);
        Ok(())
    }

    /// Lowercase hex digest of everything hashed so far.
    pub fn hexdigest(&self) -> String {
        self.hasher.hexdigest()
    }

    /// Number of traces hashed.
    pub fn count(&self) -> u64 {
        self.count
    }

    /// Digest of a JSONL trace file's contents: every non-blank line is
    /// parsed strictly and re-canonicalised, so the result equals the digest
    /// of the run that wrote it.
    pub fn of_jsonl(text: &str) -> Result<TraceDigest, IapError> {
        let mut digest = TraceDigest::new();
        for line in text.lines() {
            if line.trim().is_empty() {
                continue;
            }
            digest.update(&DecisionTrace::from_json_line(line)?)?;
        }
        Ok(digest)
    }
}

/// Trace sink writing one canonical JSON line per trace to any
/// `std::io::Write`, keeping the stream digest (`iap.trace.sinks.JsonlTraceSink`).
#[derive(Debug)]
pub struct JsonlTraceSink<W: Write> {
    sink: W,
    digest: TraceDigest,
}

impl<W: Write> JsonlTraceSink<W> {
    /// Wrap a writer (file, `Vec<u8>`, ...).
    pub fn new(sink: W) -> JsonlTraceSink<W> {
        JsonlTraceSink {
            sink,
            digest: TraceDigest::new(),
        }
    }

    /// Validate, canonicalise, write `line + "\n"`, flush, and fold the line
    /// into the digest. Nothing is written when the trace is invalid.
    pub fn emit(&mut self, trace: &DecisionTrace) -> Result<(), IapError> {
        let line = trace.to_canonical_line()?;
        self.sink.write_all(line.as_bytes())?;
        self.sink.write_all(b"\n")?;
        self.sink.flush()?;
        self.digest.update_line(&line);
        Ok(())
    }

    /// The running digest.
    pub fn digest(&self) -> &TraceDigest {
        &self.digest
    }

    /// Number of traces emitted.
    pub fn count(&self) -> u64 {
        self.digest.count()
    }

    /// Consume the sink, returning the writer.
    pub fn into_inner(self) -> W {
        self.sink
    }
}

// --------------------------------------------------------------- explain ---

const LABEL_WIDTH: usize = 12;

fn line(label: &str, body: &str) -> String {
    let mut head = format!("{label}: ");
    while head.len() < LABEL_WIDTH {
        head.push(' ');
    }
    head + body
}

fn bps(value: f64) -> String {
    format!("{value:+.1} bps")
}

/// `{:+,d}` / `{:,d}` — decimal with thousands separators, optional forced sign.
fn thousands(value: i64, force_sign: bool) -> String {
    let digits = value.unsigned_abs().to_string();
    let mut grouped = String::with_capacity(digits.len() + digits.len() / 3 + 1);
    for (i, c) in digits.chars().enumerate() {
        if i > 0 && (digits.len() - i) % 3 == 0 {
            grouped.push(',');
        }
        grouped.push(c);
    }
    if value < 0 {
        format!("-{grouped}")
    } else if force_sign {
        format!("+{grouped}")
    } else {
        grouped
    }
}

/// Render the decision chain, one line per stage item, exactly as
/// `iap.contracts.types.explain` (pinned by
/// `tests/golden/expected_contracts_examples.json` `explain`):
///
/// ```text
/// Order 12345
/// Alpha:      EQ03  expected return = +4.2 bps  confidence = 0.81
/// Portfolio:  target = +20,000 shares
/// Risk:       ALLOW
/// Execution:  POV 15%
/// SOR:        XV1 = 45%  XV2 = 35%  XV3 = 20%
/// Fills:      18,000 / 20,000 (90.0%)
/// TCA:        IS = 2.1 bps
/// Attribution: alpha = +6.2 bps  spread = -0.8 bps  impact = -2.1 bps  fees = -0.4 bps
/// ```
///
/// Stages that did not run render `(none)`; `venue_names` maps venue ids to
/// display names, unnamed venues print their decimal id.
pub fn explain(trace: &DecisionTrace, venue_names: &BTreeMap<u16, String>) -> String {
    let st = &trace.stages;
    let parents = &st.parent_orders;
    let mut lines: Vec<String> = Vec::with_capacity(12);
    lines.push(match parents.first() {
        Some(po) => format!("Order {}", po.parent_order_id),
        None => format!("Trace {}", trace.trace_id),
    });

    let alpha_label = parents.first().map(|po| po.alpha_id.as_str());
    if st.signal.is_empty() {
        lines.push(line("Alpha", "(none)"));
    } else {
        // signal[0] is the acting signal (labelled by the order's alpha id);
        // every further signal is one of its components (its own model_version).
        for (i, sig) in st.signal.iter().enumerate() {
            let label = match alpha_label {
                Some(a) if i == 0 => a,
                _ => sig.model_version.as_str(),
            };
            lines.push(line(
                "Alpha",
                &format!(
                    "{label}  expected return = {}  confidence = {:.2}",
                    bps(sig.expected_return * 1e4),
                    sig.confidence
                ),
            ));
        }
    }

    match &st.portfolio {
        Some(p) => {
            let matching: Vec<&PortfolioLeg> = p
                .targets
                .iter()
                .filter(|leg| leg.instrument_id == trace.instrument_id)
                .collect();
            let legs: Vec<&PortfolioLeg> = if matching.is_empty() {
                p.targets.iter().collect()
            } else {
                matching
            };
            let body = match legs.first() {
                Some(leg) => format!("target = {} shares", thousands(leg.target_qty, true)),
                None => format!("{}  no target", p.solver_status.name()),
            };
            lines.push(line("Portfolio", &body));
        }
        None => lines.push(line("Portfolio", "(none)")),
    }

    if st.risk.is_empty() {
        lines.push(line("Risk", "(none)"));
    } else {
        for rd in &st.risk {
            let mut body = rd.decision.name().to_string();
            if rd.decision != Decision::Allow {
                body.push_str(&format!("  rule = {}  reason = {}", rd.rule_id, rd.reason));
            }
            lines.push(line("Risk", &body));
        }
    }

    if parents.is_empty() {
        lines.push(line("Execution", "(none)"));
    } else {
        for po in parents {
            let mut body = po.algo.name().to_string();
            if let Some(p) = po.params.get("participation") {
                body.push_str(&format!(" {:.0}%", p * 100.0));
            }
            lines.push(line("Execution", &body));
        }
    }

    let child_qty: BTreeMap<u64, i64> = st
        .child_orders
        .iter()
        .map(|c| (c.child_order_id, c.qty))
        .collect();
    let mut routed: BTreeMap<u16, i64> = BTreeMap::new();
    for vd in &st.routing {
        let qty = child_qty.get(&vd.child_order_id).copied().unwrap_or(0);
        *routed.entry(vd.venue_id).or_insert(0) += qty;
    }
    let total_routed: i64 = routed.values().sum();
    if total_routed != 0 {
        let parts: Vec<String> = routed
            .iter()
            .map(|(v, q)| {
                let name = venue_names.get(v).cloned().unwrap_or_else(|| v.to_string());
                format!("{name} = {:.0}%", 100.0 * *q as f64 / total_routed as f64)
            })
            .collect();
        lines.push(line("SOR", &parts.join("  ")));
    } else {
        lines.push(line("SOR", "(none)"));
    }

    let target_qty: i64 = parents.iter().map(|po| po.qty).sum();
    let filled: i64 = st.fills.iter().map(|er| er.filled_qty).sum();
    if target_qty != 0 {
        lines.push(line(
            "Fills",
            &format!(
                "{} / {} ({:.1}%)",
                thousands(filled, false),
                thousands(target_qty, false),
                100.0 * filled as f64 / target_qty as f64
            ),
        ));
    } else {
        lines.push(line("Fills", "(none)"));
    }

    if st.tca.is_empty() {
        lines.push(line("TCA", "(none)"));
    } else {
        for t in &st.tca {
            lines.push(line(
                "TCA",
                &format!("IS = {:.1} bps", t.implementation_shortfall_bps),
            ));
        }
    }

    match &st.attribution {
        Some(a) => lines.push(line(
            "Attribution",
            &format!(
                "alpha = {}  spread = {}  impact = {}  fees = {}",
                bps(a.alpha_bps),
                bps(a.spread_bps),
                bps(a.impact_bps),
                bps(a.fees_bps)
            ),
        )),
        None => lines.push(line("Attribution", "(none)")),
    }
    lines.join("\n")
}

#[cfg(test)]
mod tests {
    use super::*;

    fn header() -> DecisionTrace {
        DecisionTrace {
            trace_id: crate::canonical::make_trace_id("s1", 7, 10, 3),
            session_id: "s1".to_string(),
            instrument_id: 7,
            event_ts: 10,
            sequence: 3,
            data_version: "0".repeat(64),
            feature_version: "1".repeat(64),
            model_version: "2".repeat(64),
            config_version: "3".repeat(64),
            stages: TraceStages::empty(),
        }
    }

    #[test]
    fn empty_stages_explain_and_round_trip() {
        let t = header();
        let text = explain(&t, &BTreeMap::new());
        let want = format!(
            "Trace {}\nAlpha:      (none)\nPortfolio:  (none)\nRisk:       (none)\nExecution:  (none)\nSOR:        (none)\nFills:      (none)\nTCA:        (none)\nAttribution: (none)",
            t.trace_id
        );
        assert_eq!(text, want);
        let line = t.to_canonical_line().expect("valid");
        assert!(line.contains("\"attribution\":null"));
        assert!(line.contains("\"portfolio\":null"));
        let back = DecisionTrace::from_json_line(&line).expect("parses");
        assert_eq!(back, t);
    }

    #[test]
    fn strictness_and_digest() {
        let mut t = header();
        t.trace_id = "00".repeat(16);
        assert!(t.validate().is_err());
        let bad = r#"{"trace_id":"x"}"#;
        assert!(DecisionTrace::from_json_line(bad).is_err());
        let t = header();
        let mut line = t.to_canonical_line().expect("valid");
        line = line.replacen("\"attribution\":null,", "", 1);
        assert!(
            DecisionTrace::from_json_line(&line).is_err(),
            "missing key must fail"
        );
        let mut sink = JsonlTraceSink::new(Vec::new());
        sink.emit(&t).expect("emit");
        sink.emit(&t).expect("emit");
        let text = String::from_utf8(sink.into_inner()).expect("ascii");
        let d = TraceDigest::of_jsonl(&text).expect("re-read");
        assert_eq!(d.count(), 2);
        let mut m = TraceDigest::new();
        m.update(&t).expect("valid");
        m.update(&t).expect("valid");
        assert_eq!(m.hexdigest(), d.hexdigest());
        assert_eq!(
            TraceDigest::new().hexdigest(),
            "e3b0c44298fc1c149afbf4c8996fb92427ae41e4649b934ca495991b7852b855"
        );
    }

    #[test]
    fn helpers() {
        assert_eq!(thousands(20000, true), "+20,000");
        assert_eq!(thousands(-1234567, true), "-1,234,567");
        assert_eq!(thousands(999, false), "999");
        assert_eq!(thousands(0, true), "+0");
        assert_eq!(bps(4.2), "+4.2 bps");
        assert_eq!(bps(-0.8), "-0.8 bps");
        assert_eq!(line("Alpha", "x"), "Alpha:      x");
        assert_eq!(line("Attribution", "x"), "Attribution: x");
        assert_eq!(Decision::try_from(3).ok(), Some(Decision::Kill));
        assert!(Direction::try_from(2).is_err());
    }
}
