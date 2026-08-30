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
    TcaGoldenTest.class,
    TcaMetricsTest.class,
    MetricsTest.class,
    ApiServerTest.class,
    ConfigServiceTest.class,
    PsiTest.class,
    AdaptiveGoldenTest.class,
    BaselineLoaderTest.class,
    DriftMonitorTest.class,
    RollingIcTest.class,
    LifecycleGaugeTest.class,
    PaperTradingSmokeTest.class,
})
public class AllTests {
}
