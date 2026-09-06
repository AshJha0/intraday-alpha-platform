package com.iap;

import com.iap.config.ConfigService;
import com.iap.risk.RiskEngine;

/** Risk engines built from the repository configs, for platform tests. */
public final class RiskEngines {
    private RiskEngines() {
    }

    /** An engine with the committed limits, armed and trading. */
    public static RiskEngine armed() {
        ConfigService cfg = new ConfigService(PaperFixtures.configs());
        return RiskEngine.fromConfig(cfg.riskDoc(), cfg.riskInstruments());
    }
}
