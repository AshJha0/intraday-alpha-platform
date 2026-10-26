# v1.18 result artefacts: index (file, producing prereg, signing commit, actor, sha256)

Generated 2026-10-20 from the run outputs in the owner's worktrees; copied byte for byte, nothing edited. Signature key: `ashjha0` for every prereg. All results here are **underpowered, inconclusive or negative; none promotes a signal** (see `archive/docs/release/v1.18.0_RELEASE_NOTES.md` in draft PR #320).

**Known limitations (see `docs/release/F7_REPRODUCTION_GUIDE.md`):** the files contain absolute `C:\Work\Claude\...` paths and run timestamps, so they are not byte-reproducible on another machine; `run_manifest.json` files are not reproducible by design; the code under `python/src/iap` on current main differs from the signing commit, so a re-run must check out the signing commit listed here.

The sha256 of 36 of the 40 files were cross-checked against the table in the F7 guide and all match. The B8 v2 `run_manifest.json` and the three REL-08c files (`DECISION.json`, `REPORT.md`, `run_manifest.json`) are not in that table; their hashes below are new.

## `research/execution/b8v2/`
- prereg: `research/preregs/b8v2_prereg.json`
- signing / bound commit: `4980fc11bd7542ab238af4577554058279505382`
- run actor: b8v2-runner; run: 2026-10-19 09:36 to 2026-10-20 01:00 UTC

| file | sha256 |
|---|---|
| `BENCHMARK_v2_oos3_AAPL.md` | `8655e1ea41f6cb6a2a57f64d5c194234a5d0e16ef92e5adcfcfcd440454f092e` |
| `BENCHMARK_v2_oos3_MSFT.md` | `2da470a614cb3990b57da9c5f620e4ddb135da44ccb93e1f9b675ab6954c4faa` |
| `BENCHMARK_v2_oos3_s1_no_hidden_AAPL.md` | `75889ac2e3bc428418d7ef4b1fff9cafacd81ef191e2b3f680ab78dca652f2af` |
| `BENCHMARK_v2_oos3_s1_no_hidden_MSFT.md` | `1ea7462c36377e0e1ab655f4a9d1998271d598771df55ea9f529d2b3009e717f` |
| `BENCHMARK_v2_oos3_s2_child_impact_AAPL.md` | `ad9c97f3ea6ca768f2f2eb3b6de72fa0e634871de4e5933be35fbcb0d331eb9f` |
| `BENCHMARK_v2_oos3_s2_child_impact_MSFT.md` | `255ef830e7a6713cd6e57d62450e100f308185480046f5a5d8230c3fdcd1c877` |
| `BENCHMARK_v2_oos3_s3_v117_child_AAPL.md` | `5e3f9dc158ec24b2584da4f993a37ff8d9d1ac69a194a0196a071c293a92bb9b` |
| `BENCHMARK_v2_oos3_s3_v117_child_MSFT.md` | `4c0ae9906fec186ce351fe58f26034bb1e0a11c8670142b0f0ced4a1eb69ac7e` |
| `DECISION.json` | `eb04cd67e147b3a25f5bfbd319a461fd30955c9ff530f9a37659c3ae396613d3` |
| `REPORT.md` | `e292cfe47eb6f2da42133020d86707515faaf80900be68a5c6cdb9f56b77b954` |
| `benchmark_v2_oos3_AAPL.json` | `6b43f80728f56c8eb169634efba7bcdecf49b640b04d9e7b6dc47ecb7b05d4d4` |
| `benchmark_v2_oos3_MSFT.json` | `24cec5c6b24da44e9f6b3700c2bf679d738884ab52149506e3f2a3cc8b526970` |
| `benchmark_v2_oos3_s1_no_hidden_AAPL.json` | `cdad9777b9c72795c53776e6038577eb00eac570f7de0e144ee5897944ab58fa` |
| `benchmark_v2_oos3_s1_no_hidden_MSFT.json` | `11131ed3fd3d178a456c4f1e91a1f2b52c4ab8c6d5740fdae947e4118af66b54` |
| `benchmark_v2_oos3_s2_child_impact_AAPL.json` | `f9ff9ed92898e09b03d9c4b81699aaef98f62df0808f20154cf5147c0d800487` |
| `benchmark_v2_oos3_s2_child_impact_MSFT.json` | `7bbb1d416310d05b9e634889b38da1effa1f1bfadc7fde95591ae4ed276b99bd` |
| `benchmark_v2_oos3_s3_v117_child_AAPL.json` | `01fee693102f9a3db2baea724c8740f375a894ed1f6429eff28b64bc824220f7` |
| `benchmark_v2_oos3_s3_v117_child_MSFT.json` | `32a63354327394606b4a1d216403013223e028966c491d160fb1a26c1eaa3c50` |
| `run_manifest.json` | `3becada307c564e07ed3cb2a0e3e4d8b6259ee1812f4223698c5dfc0b203ae6f` |

## `research/execution/rel05/`
- prereg: `research/preregs/rel05_prereg.json`
- signing / bound commit: `aa2d4b8242bd9cc3db92d44d8bd1653e3f14721a`
- run actor: claude-queue-runner; run: 2026-10-20 13:13 to 13:24 UTC

| file | sha256 |
|---|---|
| `DECISION.json` | `4d20e8aa029606d4d611a5635dde8be693440f98de8e65b8a87eeeecdd07c860` |
| `REPORT.md` | `29d3f58eeac2a1388bb293eac7371cba352e8c7013570d83a3ada13146d7f155` |
| `run_manifest.json` | `b84ec9105f6d2fea975b38eef1dec4bea6325a178cb20b0d62a232058d06df70` |
| `tape_AAPL.json` | `13a5a3d493768be0b6c8b809eaaee5b6100c54ada771fc846f6e6ff77a5e6fef` |
| `tape_MSFT.json` | `d307e91c37c4ef41e7fc9d04b82c1df1b04ecead26c750d61f2a49c7e0e26434` |
| `tape_QQQ.json` | `6206a6573161b9d1a2c892d684d51505524941940cd65bfa9b3e861db9774a64` |

## `research/v118/rel09/`
- prereg: `research/preregs/rel09_prereg.json`
- signing / bound commit: `6dc3783c907231f0bc9ee1e28f2d3ee86909dec3`
- run actor: claude-queue-runner; run: 2026-10-20 11:17 to 12:32 UTC

| file | sha256 |
|---|---|
| `DECISION.json` | `e56757a6d752d94cb7bc5c4d3c9ddc852b78165b8e89db8e5368e3448fae394f` |
| `REPORT.md` | `b938e2e6c3918b393c1ce4cee09a1fbd9d8bbfd47739261fff82db10cf2dc294` |
| `rows_calibrate.json` | `e896f28d4501103e1e181ab190cefe43281c173feecd9cbaa6c188b2c8030003` |
| `rows_evaluate.json` | `d23a543679c5845c42c32f9f7576c251c7fbe9e744a8ee4a4a0a3bcecf208dbb` |
| `run_manifest.json` | `950be4b5e518057f0b8576aaac3583df3ea00a2cc31d76170e93386254dc477f` |

## `research/v118/rel10/`
- prereg: `research/preregs/rel10_prereg.json`
- signing / bound commit: `620a1c2ce9f645f2391872fa858b04b37c8d0c6e`
- run actor: claude-queue-runner; run: 2026-10-20 12:33 to 12:55 UTC

| file | sha256 |
|---|---|
| `DECISION.json` | `e0513ea0fd84ed42d5160e1277ff1ddfb93abe94626d3678eb1417dac6725c04` |
| `REPORT.md` | `c3a61984cc2f756de3e3d84ea25076882f9a1e329602799b28742a5c901276d4` |
| `calibration.json` | `b5933edd9f368ab6c84bf3480c0a49382bfbf453da99f632fac888eabbe685b1` |
| `run_manifest.json` | `a9b2b42a25ca2edf44e248821629f92460522d01b44b0b12e08a225501da1adb` |
| `trips_evaluate.json` | `eceeeef8d84b5017ae352ad28b2e0ec3c393de57d58fc3102b0402be322de312` |

## `research/v118/rel08/`
- prereg: `research/v118/rel08_rule_signed.json (rule, message sha256 b44502181b59...)`
- signing / bound commit: `739a7e069dceeec32cec37de7a4443730eeac88a`
- run actor: not recorded in a run manifest (see data/real/run_rel08_screen.log); run: 2026-10-20 07:52 to 07:53 UTC

| file | sha256 |
|---|---|
| `SCORECARD_ds_20190130.md` | `ecb14c27303de35a7c3250dde8fa8b698e5d187d69fd586e4e74c009c2a6dce2` |
| `screen_ds_20190130.json` | `37bbe74f92bc52459cdabed0efd1dec30914f09ffa997c869f16fc26be9cd30b` |

## `research/auction/rel08c/`
- prereg: `research/preregs/rel08c_prereg.json (study DROPPED, see research/v118/rel08c/DROPPED.md in draft PR #316)`
- signing / bound commit: `67db845a4f97572a91fee5ab93e54061453439f6`
- run actor: claude-queue-runner; run: 2026-10-20 06:17 to 07:38 UTC

| file | sha256 |
|---|---|
| `DECISION.json` | `08b655f843d880d9cc738a5d7b3ea3edbf963d3414e2348f4b7168426c1ee2a3` |
| `REPORT.md` | `7e2a1845ab7d432f9b74440a8fc39eb4ef164a81498366d6541ba22f47886f15` |
| `run_manifest.json` | `f13b418e2a819f06972eb9e37cef85e40db4c8c80bdda9f4aae497b7d6679f62` |

