package com.iap;

import org.junit.runner.RunWith;
import org.junit.runners.Suite;

/** Full suite (includes the golden group; see test.sh). */
@RunWith(Suite.class)
@Suite.SuiteClasses({
    SplitMix64Test.class,
    ValidationTest.class,
    JsonlCodecTest.class,
    Iap1CodecTest.class,
    CodecGoldenTest.class,
    BookSemanticsTest.class,
    BookSequencingTest.class,
    BookGoldenTest.class,
    CheckpointTest.class,
    ConsolidatedBookTest.class,
    ReplayTest.class,
    ScenarioCoreTest.class,
    AnomalyGoldenTest.class,
    FeatureGoldenTest.class,
    FeatureBruteTest.class,
    AlphaGoldenTest.class,
    ExecutionSimTest.class,
    AlgosTest.class,
    ReplayFillsGoldenTest.class,
    BacktestTest.class,
    PortfolioGoldenTest.class,
    PortfolioSolverTest.class,
    RiskGoldenTest.class,
    RiskRuleTest.class,
    RiskScenarioTest.class,
    ExecutionScenarioTest.class,
    PaperRiskWiringTest.class,
    TcaGoldenTest.class,
    TcaMetricsTest.class,
    MetricsTest.class,
    MetricsExpositionTest.class,
    MetricsConcurrencyTest.class,
    ApiServerTest.class,
    ApiEndpointTest.class,
    AdminApiTest.class,
    ConfigServiceTest.class,
    PlatformConfigTest.class,
    PsiTest.class,
    AdaptiveGoldenTest.class,
    BaselineLoaderTest.class,
    DriftMonitorTest.class,
    RollingIcTest.class,
    LifecycleGaugeTest.class,
    PaperTradingSmokeTest.class,
    PaperUnitsTest.class,
    PaperObservabilityTest.class,
    PaperStateRecoveryTest.class,
})
public class AllTests {
}
