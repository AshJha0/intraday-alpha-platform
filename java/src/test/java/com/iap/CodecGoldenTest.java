package com.iap;

import static org.junit.Assert.assertArrayEquals;
import static org.junit.Assert.assertEquals;

import java.util.List;
import java.util.Map;

import org.junit.Test;

import com.iap.codec.Iap1Codec;
import com.iap.codec.JsonlCodec;
import com.iap.codec.Sha256;
import com.iap.core.MarketEvent;

/**
 * Golden codec parity (API_CORE.md section 3): JSONL re-encoding is
 * byte-identical to the golden files, and the IAP1 encoding hashes to
 * expected_codec_sha256.json exactly.
 */
public class CodecGoldenTest {

    @Test
    public void eqJsonlReencodesByteExact() {
        assertArrayEquals(Golden.bytes("events_eq_mbo.jsonl"),
                JsonlCodec.encode(Golden.eq()));
    }

    @Test
    public void fxJsonlReencodesByteExact() {
        assertArrayEquals(Golden.bytes("events_fx_quote.jsonl"),
                JsonlCodec.encode(Golden.fx()));
    }

    @Test
    public void eqIap1Sha256Matches() {
        Map<String, Object> expected = Golden.json("expected_codec_sha256.json");
        assertEquals(expected.get("events_eq_mbo.iap1"),
                Sha256.hex(Iap1Codec.encode(Golden.eq())));
    }

    @Test
    public void fxIap1Sha256Matches() {
        Map<String, Object> expected = Golden.json("expected_codec_sha256.json");
        assertEquals(expected.get("events_fx_quote.iap1"),
                Sha256.hex(Iap1Codec.encode(Golden.fx())));
    }

    @Test
    public void eqIap1RoundTripsLosslessly() {
        List<MarketEvent> events = Golden.eq();
        assertEquals(events, Iap1Codec.decode(Iap1Codec.encode(events)));
    }

    @Test
    public void fxIap1RoundTripsLosslessly() {
        List<MarketEvent> events = Golden.fx();
        assertEquals(events, Iap1Codec.decode(Iap1Codec.encode(events)));
    }
}
