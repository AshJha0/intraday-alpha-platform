# Feature drift report

_Descriptive only: never changes a pre-registration, a gate or a result._

Inputs: ref = ['C:\\Work\\Claude\\intraday-alpha-platform\\intraday-alpha-platform\\data\\real\\multi7\\features'], target = ['C:\\Work\\Claude\\intraday-alpha-platform\\intraday-alpha-platform\\data\\real\\r7_20251128\\features', 'C:\\Work\\Claude\\intraday-alpha-platform\\intraday-alpha-platform\\data\\real\\r7_20251208\\features', 'C:\\Work\\Claude\\intraday-alpha-platform\\intraday-alpha-platform\\data\\real\\r7_20251209\\features', 'C:\\Work\\Claude\\intraday-alpha-platform\\intraday-alpha-platform\\data\\real\\r7_20251210\\features', 'C:\\Work\\Claude\\intraday-alpha-platform\\intraday-alpha-platform\\data\\real\\r7_20251211\\features', 'C:\\Work\\Claude\\intraday-alpha-platform\\intraday-alpha-platform\\data\\real\\r7_20251212\\features', 'C:\\Work\\Claude\\intraday-alpha-platform\\intraday-alpha-platform\\data\\real\\r7_20260612\\features']

Thresholds: psi_warn=0.1, psi_alert=0.25, ks_alert=0.1, mean_shift_alert=0.5, std_ratio_alert=2.0, min_rows=100

## Summary

| group | features | status counts | drifted |
|---|---|---|---|
| 1 | 205 | {'drift': 106, 'ok': 92, 'warn': 7} | add_intensity_w10s_v1, add_intensity_w1m_v1, add_intensity_w1s_v1, add_qty_w10s_v1, add_qty_w1s_v1, avg_order_size_ask_l1_v1, avg_order_size_bid_l1_v1, beta_w5m_v1, cancel_add_ratio_w10s_v1, cancel_add_ratio_w1s_v1 ... |
| 2 | 205 | {'drift': 130, 'ok': 62, 'warn': 13} | add_intensity_w10s_v1, add_intensity_w1m_v1, add_intensity_w1s_v1, add_qty_w10s_v1, add_qty_w1s_v1, avg_order_size_ask_l1_v1, avg_order_size_bid_l1_v1, beta_w5m_v1, cancel_add_ratio_w10s_v1, cancel_add_ratio_w1s_v1 ... |
| 3 | 205 | {'drift': 124, 'ok': 65, 'warn': 16} | add_intensity_w10s_v1, add_intensity_w1m_v1, add_intensity_w1s_v1, add_qty_w10s_v1, add_qty_w1s_v1, avg_order_size_ask_l1_v1, avg_order_size_bid_l1_v1, cancel_add_ratio_w10s_v1, cancel_add_ratio_w1s_v1, cancel_intensity_w10s_v1 ... |
| pooled | 205 | {'drift': 122, 'ok': 73, 'warn': 10} | add_intensity_w10s_v1, add_intensity_w1m_v1, add_intensity_w1s_v1, add_qty_w10s_v1, add_qty_w1s_v1, avg_order_size_ask_l1_v1, avg_order_size_bid_l1_v1, beta_w5m_v1, cancel_add_ratio_w10s_v1, cancel_add_ratio_w1s_v1 ... |

## 1 (top 50 by PSI)

| feature | status | PSI | KS | mean shift (ref std) | std ratio | n ref | n target |
|---|---|---|---|---|---|---|---|
| mid_price_v1 | drift | 6.36 | 0.6943 | 0.8815 | 0.1033 | 959662 | 863807 |
| microprice_v1 | drift | 6.294 | 0.6943 | 0.8815 | 0.1033 | 959662 | 863807 |
| cancel_intensity_w1m_v1 | drift | 2.417 | 0.596 | 3.804 | 4.516 | 959672 | 863818 |
| add_intensity_w1m_v1 | drift | 2.31 | 0.5874 | 2.161 | 2.768 | 959672 | 863818 |
| beta_w5m_v1 | drift | 2.232 | 0.6646 | -0.1136 | 25.26 | 954792 | 860028 |
| quoted_depth_avg_w1m_v1 | drift | 1.927 | 0.4874 | -0.8524 | 0.5732 | 959652 | 863806 |
| quoted_depth_avg_w10s_v1 | drift | 1.833 | 0.4768 | -0.7984 | 0.6615 | 959522 | 863485 |
| avg_order_size_ask_l1_v1 | drift | 1.781 | 0.5842 | -0.327 | 0.4047 | 959662 | 863807 |
| depth_total_l10_v1 | drift | 1.779 | 0.4751 | -0.7609 | 0.6982 | 959662 | 863807 |
| quoted_depth_total_v1 | drift | 1.779 | 0.4751 | -0.7609 | 0.6982 | 959662 | 863807 |
| norm_depth_m5_v1 | drift | 1.754 | 0.5322 | 1.01 | 1.389 | 958222 | 854087 |
| depth_ask_l10_avg_w10s_v1 | drift | 1.717 | 0.4624 | -0.6211 | 0.5165 | 959522 | 863485 |
| depth_bid_l10_avg_w10s_v1 | drift | 1.699 | 0.4591 | -0.7939 | 1.123 | 959522 | 863485 |
| cancel_intensity_w10s_v1 | drift | 1.696 | 0.538 | 3.317 | 4.533 | 959672 | 863818 |
| add_intensity_w10s_v1 | drift | 1.663 | 0.5327 | 1.209 | 1.942 | 959672 | 863818 |
| depth_ask_l10_avg_w1s_v1 | drift | 1.662 | 0.4555 | -0.5859 | 0.545 | 958881 | 862060 |
| depth_ask_l10_v1 | drift | 1.65 | 0.4568 | -0.5738 | 0.5571 | 959662 | 863807 |
| depth_bid_l10_avg_w1s_v1 | drift | 1.583 | 0.4453 | -0.76 | 1.146 | 958881 | 862060 |
| depth_bid_l10_v1 | drift | 1.567 | 0.4451 | -0.752 | 1.161 | 959662 | 863807 |
| avg_order_size_bid_l1_v1 | drift | 1.499 | 0.5242 | -0.2717 | 0.7575 | 959662 | 863807 |
| depth_ask_l5_avg_w10s_v1 | drift | 1.276 | 0.3655 | -0.4641 | 0.4304 | 959522 | 863485 |
| depth_total_l5_v1 | drift | 1.142 | 0.3619 | -0.5621 | 0.7985 | 959662 | 863807 |
| depth_ask_l5_avg_w1s_v1 | drift | 1.14 | 0.3559 | -0.4215 | 0.4824 | 958881 | 862060 |
| trade_count_w1m_v1 | drift | 1.107 | 0.4274 | 0.9684 | 1.919 | 959672 | 863818 |
| trade_intensity_w1m_v1 | drift | 1.107 | 0.4274 | 0.9684 | 1.919 | 959672 | 863818 |
| depth_ask_l5_v1 | drift | 1.073 | 0.354 | -0.4054 | 0.5035 | 959662 | 863807 |
| depth_bid_l5_avg_w10s_v1 | drift | 1.046 | 0.3409 | -0.6237 | 1.435 | 959522 | 863485 |
| depth_ask_l1_avg_w10s_v1 | drift | 1.02 | 0.4002 | -0.2871 | 0.3514 | 959522 | 863485 |
| participation_w1m_v1 | drift | 0.9333 | 0.4083 | 0.832 | 1.022 | 959652 | 863806 |
| add_intensity_w1s_v1 | drift | 0.8872 | 0.3678 | 0.6984 | 1.642 | 959672 | 863818 |
| cancel_intensity_w1s_v1 | drift | 0.8732 | 0.3689 | 2.212 | 4.446 | 959672 | 863818 |
| depth_bid_l5_avg_w1s_v1 | drift | 0.8718 | 0.3195 | -0.557 | 1.438 | 958881 | 862060 |
| depth_bid_l5_v1 | drift | 0.8108 | 0.3219 | -0.5406 | 1.452 | 959662 | 863807 |
| execute_intensity_w1m_v1 | drift | 0.7809 | 0.3622 | 0.7912 | 1.799 | 959672 | 863818 |
| depth_bid_l1_avg_w10s_v1 | drift | 0.737 | 0.346 | -0.3841 | 1.119 | 959522 | 863485 |
| depth_total_l1_v1 | drift | 0.6467 | 0.3196 | -0.2361 | 0.6911 | 959662 | 863807 |
| norm_volume_m5_v1 | drift | 0.6426 | 0.3236 | 0.3319 | 4.694 | 958177 | 852898 |
| trade_count_w10s_v1 | drift | 0.585 | 0.3262 | 0.6637 | 1.902 | 959672 | 863818 |
| trade_intensity_w10s_v1 | drift | 0.585 | 0.3262 | 0.6637 | 1.902 | 959672 | 863818 |
| participation_w10s_v1 | drift | 0.5836 | 0.3278 | 0.8448 | 1.287 | 959522 | 863485 |
| depth_ask_l1_v1 | drift | 0.5399 | 0.3553 | -0.1616 | 0.4163 | 959662 | 863807 |
| modify_intensity_w10s_v1 | drift | 0.5274 | 0.2515 | -0.4287 | 0.3995 | 959672 | 863818 |
| depth_ask_l1_avg_w1s_v1 | drift | 0.5262 | 0.3096 | -0.2158 | 0.4171 | 958881 | 862060 |
| queue_depletion_rate_ask_w10s_v1 | drift | 0.5012 | 0.2697 | -0.4184 | 0.8622 | 959672 | 863818 |
| corr_contemp_w5m_v1 | drift | 0.4989 | 0.2769 | -0.5585 | 1.087 | 954792 | 860028 |
| depth_bid_l1_avg_w1s_v1 | drift | 0.4847 | 0.2871 | -0.2519 | 1.22 | 958881 | 862060 |
| cancel_add_ratio_w10s_v1 | drift | 0.429 | 0.2435 | 0.09065 | 1.569 | 957048 | 861069 |
| cancel_add_ratio_w1s_v1 | drift | 0.4254 | 0.1415 | -0.01386 | 1.52 | 945499 | 849340 |
| execute_intensity_w10s_v1 | drift | 0.3987 | 0.2678 | 0.5241 | 1.743 | 959672 | 863818 |
| add_qty_w10s_v1 | drift | 0.395 | 0.2458 | 0.3988 | 1.056 | 959672 | 863818 |

## 2 (top 50 by PSI)

| feature | status | PSI | KS | mean shift (ref std) | std ratio | n ref | n target |
|---|---|---|---|---|---|---|---|
| mid_price_v1 | drift | 8.371 | 1 | 15.02 | 1.742 | 921342 | 713384 |
| microprice_v1 | drift | 8.34 | 1 | 15.02 | 1.755 | 921342 | 713384 |
| depth_bid_l10_avg_w10s_v1 | drift | 7.441 | 0.9587 | -1.763 | 0.1287 | 921223 | 713077 |
| quoted_depth_avg_w1m_v1 | drift | 7.178 | 0.9709 | -2.225 | 0.1818 | 921340 | 713380 |
| depth_bid_l10_avg_w1s_v1 | drift | 7.136 | 0.9512 | -1.721 | 0.1444 | 920838 | 711157 |
| depth_bid_l5_avg_w10s_v1 | drift | 7.107 | 0.9522 | -1.453 | 0.1062 | 921223 | 713077 |
| depth_total_l10_v1 | drift | 7.047 | 0.9551 | -2.082 | 0.2228 | 921342 | 713384 |
| quoted_depth_total_v1 | drift | 7.047 | 0.9551 | -2.082 | 0.2228 | 921342 | 713384 |
| quoted_depth_avg_w10s_v1 | drift | 7.046 | 0.9627 | -2.132 | 0.2019 | 921223 | 713077 |
| depth_bid_l10_v1 | drift | 7.03 | 0.9477 | -1.709 | 0.1493 | 921342 | 713384 |
| depth_bid_l5_avg_w1s_v1 | drift | 6.649 | 0.9352 | -1.385 | 0.1317 | 920838 | 711157 |
| depth_total_l5_v1 | drift | 6.578 | 0.9467 | -1.723 | 0.2188 | 921342 | 713384 |
| depth_ask_l10_avg_w10s_v1 | drift | 6.501 | 0.9521 | -1.888 | 0.2975 | 921223 | 713077 |
| depth_bid_l5_v1 | drift | 6.467 | 0.9276 | -1.361 | 0.1433 | 921342 | 713384 |
| depth_ask_l5_avg_w10s_v1 | drift | 6.453 | 0.9448 | -1.63 | 0.2843 | 921223 | 713077 |
| depth_ask_l10_avg_w1s_v1 | drift | 6.361 | 0.9455 | -1.828 | 0.3158 | 920838 | 711157 |
| depth_ask_l10_v1 | drift | 6.355 | 0.9422 | -1.807 | 0.324 | 921342 | 713384 |
| depth_ask_l5_avg_w1s_v1 | drift | 6.116 | 0.9267 | -1.533 | 0.3235 | 920838 | 711157 |
| depth_ask_l5_v1 | drift | 5.976 | 0.9159 | -1.497 | 0.335 | 921342 | 713384 |
| depth_bid_l1_avg_w10s_v1 | drift | 5.458 | 0.9276 | -0.7364 | 0.09397 | 921223 | 713077 |
| depth_ask_l1_avg_w10s_v1 | drift | 5.343 | 0.906 | -1.195 | 0.2169 | 921223 | 713077 |
| depth_total_l1_v1 | drift | 4.701 | 0.8428 | -0.7749 | 0.2251 | 921342 | 713384 |
| depth_bid_l1_avg_w1s_v1 | drift | 4.653 | 0.819 | -0.5571 | 0.1263 | 920838 | 711157 |
| depth_ask_l1_avg_w1s_v1 | drift | 4.31 | 0.815 | -0.8561 | 0.2739 | 920838 | 711157 |
| jump_count_w5m_v1 | drift | 4.106 | 0.8303 | 2.928 | 5.556 | 921363 | 713396 |
| avg_order_size_ask_l1_v1 | drift | 3.485 | 0.796 | -0.6167 | 0.5587 | 921342 | 713384 |
| avg_order_size_bid_l1_v1 | drift | 3.323 | 0.7848 | -0.3316 | 0.2758 | 921342 | 713384 |
| depth_bid_l1_v1 | drift | 3.311 | 0.7239 | -0.4907 | 0.1589 | 921342 | 713384 |
| depth_ask_l1_v1 | drift | 3.132 | 0.719 | -0.7184 | 0.3466 | 921342 | 713384 |
| participation_w1m_v1 | drift | 3.099 | 0.6816 | 1.701 | 0.9933 | 921340 | 713380 |
| queue_depletion_rate_bid_w10s_v1 | drift | 2.7 | 0.6805 | -0.8058 | 0.3707 | 921363 | 713396 |
| queue_depletion_rate_ask_w10s_v1 | drift | 2.5 | 0.6644 | -0.8018 | 0.4581 | 921363 | 713396 |
| queue_replenish_rate_bid_w10s_v1 | drift | 2.35 | 0.6341 | -0.7516 | 0.3957 | 921363 | 713396 |
| jump_flag_w1m_v1 | drift | 2.213 | 0.5289 | 3.972 | 3.738 | 873450 | 680766 |
| participation_w10s_v1 | drift | 2.088 | 0.5936 | 2.024 | 1.675 | 921223 | 713077 |
| queue_replenish_rate_ask_w10s_v1 | drift | 2.08 | 0.6039 | -0.7276 | 0.4678 | 921363 | 713396 |
| ofi_l1_w30s_v1 | drift | 1.952 | 0.29 | 0.002048 | 0.2251 | 921363 | 713396 |
| ofi_l3_w30s_v1 | drift | 1.919 | 0.2893 | -0.0003877 | 0.3233 | 921363 | 713396 |
| ofi_l5_w30s_v1 | drift | 1.868 | 0.2889 | -0.0003941 | 0.3438 | 921363 | 713396 |
| ofi_l1_w5s_v1 | drift | 1.845 | 0.2847 | 0.002293 | 0.2653 | 921363 | 713396 |
| ofi_l3_w5s_v1 | drift | 1.661 | 0.2722 | 0.0004979 | 0.3707 | 921363 | 713396 |
| ofi_l10_w30s_v1 | drift | 1.62 | 0.2733 | -0.001036 | 0.3559 | 921363 | 713396 |
| ofi_l5_w5s_v1 | drift | 1.577 | 0.2693 | -8.136e-05 | 0.4095 | 921363 | 713396 |
| ofi_l1_w1s_v1 | drift | 1.542 | 0.268 | 0.001948 | 0.3294 | 921363 | 713396 |
| modify_intensity_w10s_v1 | drift | 1.441 | 0.4401 | -0.6919 | 0.3456 | 921363 | 713396 |
| ofi_l3_w1s_v1 | drift | 1.421 | 0.2571 | 0.0001933 | 0.3945 | 921363 | 713396 |
| ofi_l10_w5s_v1 | drift | 1.395 | 0.2585 | -0.0002436 | 0.3997 | 921363 | 713396 |
| ofi_l5_w1s_v1 | drift | 1.36 | 0.2475 | -0.0001805 | 0.418 | 921363 | 713396 |
| ofi_l10_w1s_v1 | drift | 1.23 | 0.2378 | -0.001228 | 0.413 | 921363 | 713396 |
| trade_count_w1m_v1 | drift | 1.175 | 0.4273 | 1.074 | 2.274 | 921363 | 713396 |

## 3 (top 50 by PSI)

| feature | status | PSI | KS | mean shift (ref std) | std ratio | n ref | n target |
|---|---|---|---|---|---|---|---|
| microprice_v1 | drift | 8.363 | 1 | 23.83 | 2.046 | 1219117 | 1691959 |
| mid_price_v1 | drift | 8.353 | 1 | 23.83 | 2.046 | 1219117 | 1691959 |
| depth_ask_l5_avg_w10s_v1 | drift | 7.49 | 0.9426 | -1.795 | 0.2404 | 1219118 | 1691884 |
| depth_bid_l5_avg_w10s_v1 | drift | 7.471 | 0.9389 | -1.787 | 0.2469 | 1219118 | 1691884 |
| depth_total_l5_v1 | drift | 7.458 | 0.9404 | -1.859 | 0.2585 | 1219117 | 1691959 |
| depth_ask_l5_avg_w1s_v1 | drift | 7.235 | 0.931 | -1.743 | 0.2505 | 1219109 | 1691493 |
| quoted_depth_avg_w1m_v1 | drift | 7.222 | 0.9379 | -2.21 | 0.2622 | 1219118 | 1691960 |
| quoted_depth_avg_w10s_v1 | drift | 7.207 | 0.9336 | -2.183 | 0.2642 | 1219118 | 1691884 |
| depth_total_l10_v1 | drift | 7.192 | 0.9321 | -2.179 | 0.2753 | 1219117 | 1691959 |
| quoted_depth_total_v1 | drift | 7.192 | 0.9321 | -2.179 | 0.2753 | 1219117 | 1691959 |
| depth_bid_l5_avg_w1s_v1 | drift | 7.143 | 0.9261 | -1.75 | 0.2568 | 1219109 | 1691493 |
| depth_ask_l5_v1 | drift | 7.056 | 0.9226 | -1.716 | 0.2622 | 1219117 | 1691959 |
| depth_bid_l5_v1 | drift | 6.967 | 0.918 | -1.725 | 0.2672 | 1219117 | 1691959 |
| depth_bid_l1_avg_w10s_v1 | drift | 6.881 | 0.8964 | -1.36 | 0.2293 | 1219118 | 1691884 |
| depth_ask_l10_avg_w10s_v1 | drift | 6.878 | 0.9273 | -2.051 | 0.2731 | 1219118 | 1691884 |
| depth_bid_l10_avg_w10s_v1 | drift | 6.83 | 0.9317 | -2.058 | 0.2654 | 1219118 | 1691884 |
| depth_ask_l1_avg_w10s_v1 | drift | 6.762 | 0.8961 | -1.506 | 0.2612 | 1219118 | 1691884 |
| depth_bid_l10_avg_w1s_v1 | drift | 6.716 | 0.9275 | -2.035 | 0.2754 | 1219109 | 1691493 |
| depth_bid_l10_v1 | drift | 6.692 | 0.9265 | -2.036 | 0.2842 | 1219117 | 1691959 |
| depth_ask_l10_avg_w1s_v1 | drift | 6.67 | 0.9249 | -2.011 | 0.2827 | 1219109 | 1691493 |
| depth_ask_l10_v1 | drift | 6.618 | 0.9243 | -2.009 | 0.2919 | 1219117 | 1691959 |
| depth_bid_l1_avg_w1s_v1 | drift | 5.047 | 0.783 | -1.025 | 0.2233 | 1219109 | 1691493 |
| depth_ask_l1_avg_w1s_v1 | drift | 4.837 | 0.7761 | -1.084 | 0.2427 | 1219109 | 1691493 |
| depth_total_l1_v1 | drift | 4.47 | 0.7602 | -1.154 | 0.2882 | 1219117 | 1691959 |
| add_intensity_w1m_v1 | drift | 2.842 | 0.6864 | 7.045 | 7.547 | 1219125 | 1691968 |
| depth_bid_l1_v1 | drift | 2.834 | 0.6115 | -0.8047 | 0.2725 | 1219117 | 1691959 |
| cancel_intensity_w1m_v1 | drift | 2.829 | 0.6885 | 7.219 | 7.726 | 1219125 | 1691968 |
| depth_ask_l1_v1 | drift | 2.783 | 0.5974 | -0.8376 | 0.295 | 1219117 | 1691959 |
| add_intensity_w10s_v1 | drift | 2.52 | 0.6626 | 6.255 | 7.006 | 1219125 | 1691968 |
| cancel_intensity_w10s_v1 | drift | 2.52 | 0.664 | 6.535 | 7.287 | 1219125 | 1691968 |
| execute_intensity_w1m_v1 | drift | 2.478 | 0.6028 | 3.38 | 4.353 | 1219125 | 1691968 |
| trade_count_w1m_v1 | drift | 2.389 | 0.6012 | 3.325 | 4.299 | 1219125 | 1691968 |
| trade_intensity_w1m_v1 | drift | 2.389 | 0.6012 | 3.325 | 4.299 | 1219125 | 1691968 |
| participation_w1m_v1 | drift | 2.265 | 0.6758 | 2.609 | 1.805 | 1219118 | 1691960 |
| avg_order_size_bid_l1_v1 | drift | 2.209 | 0.5982 | -0.6544 | 0.3468 | 1219117 | 1691959 |
| participation_w10s_v1 | drift | 2.115 | 0.621 | 3.531 | 3.315 | 1219118 | 1691884 |
| avg_order_size_ask_l1_v1 | drift | 2.111 | 0.5994 | -0.6644 | 0.4322 | 1219117 | 1691959 |
| effective_spread_bps_w1m_v1 | drift | 1.811 | 0.5288 | -0.3682 | 0.85 | 1158343 | 1680151 |
| cancel_intensity_w1s_v1 | drift | 1.684 | 0.5694 | 5.072 | 6.38 | 1219125 | 1691968 |
| add_intensity_w1s_v1 | drift | 1.679 | 0.5693 | 4.812 | 6.092 | 1219125 | 1691968 |
| execute_intensity_w10s_v1 | drift | 1.631 | 0.5374 | 2.363 | 3.636 | 1219125 | 1691968 |
| ofi_l1_w30s_v1 | drift | 1.608 | 0.2228 | 0.001417 | 0.2795 | 1219125 | 1691968 |
| trade_count_w10s_v1 | drift | 1.6 | 0.536 | 2.35 | 3.65 | 1219125 | 1691968 |
| trade_intensity_w10s_v1 | drift | 1.6 | 0.536 | 2.35 | 3.65 | 1219125 | 1691968 |
| ofi_l1_w5s_v1 | drift | 1.549 | 0.2169 | 0.001434 | 0.2829 | 1219125 | 1691968 |
| ofi_l1_w1s_v1 | drift | 1.342 | 0.1865 | 0.001985 | 0.3018 | 1219125 | 1691968 |
| ofi_l3_w30s_v1 | drift | 1.332 | 0.2154 | 0.003657 | 0.2896 | 1219125 | 1691968 |
| effective_spread_bps_w10s_v1 | drift | 1.261 | 0.4284 | -0.1855 | 0.8294 | 1028196 | 1557296 |
| ofi_l3_w5s_v1 | drift | 1.21 | 0.2026 | 0.002493 | 0.3082 | 1219125 | 1691968 |
| jump_count_w5m_v1 | drift | 1.209 | 0.4119 | 8.598 | 27.77 | 1219125 | 1691968 |

## pooled (top 50 by PSI)

| feature | status | PSI | KS | mean shift (ref std) | std ratio | n ref | n target |
|---|---|---|---|---|---|---|---|
| mid_price_v1 | drift | 8.293 | 0.9054 | 6.334 | 3.1 | 3100121 | 3269150 |
| microprice_v1 | drift | 8.29 | 0.9054 | 6.334 | 3.1 | 3100121 | 3269150 |
| quoted_depth_avg_w1m_v1 | drift | 3.487 | 0.6364 | -0.9825 | 0.2266 | 3100110 | 3269146 |
| quoted_depth_avg_w10s_v1 | drift | 3.4 | 0.6286 | -0.9763 | 0.2352 | 3099863 | 3268446 |
| depth_total_l10_v1 | drift | 3.242 | 0.613 | -0.9681 | 0.2445 | 3100121 | 3269150 |
| quoted_depth_total_v1 | drift | 3.242 | 0.613 | -0.9681 | 0.2445 | 3100121 | 3269150 |
| depth_ask_l10_avg_w10s_v1 | drift | 3.068 | 0.6144 | -0.9571 | 0.2469 | 3099863 | 3268446 |
| depth_bid_l10_avg_w10s_v1 | drift | 3.061 | 0.6047 | -0.94 | 0.2622 | 3099863 | 3268446 |
| depth_bid_l10_avg_w1s_v1 | drift | 2.947 | 0.5862 | -0.9331 | 0.2718 | 3098828 | 3264710 |
| depth_ask_l10_avg_w1s_v1 | drift | 2.906 | 0.5983 | -0.9453 | 0.2573 | 3098828 | 3264710 |
| depth_bid_l10_v1 | drift | 2.896 | 0.5723 | -0.9295 | 0.2775 | 3100121 | 3269150 |
| depth_total_l5_v1 | drift | 2.893 | 0.4661 | -0.8212 | 0.2455 | 3100121 | 3269150 |
| depth_ask_l5_avg_w10s_v1 | drift | 2.892 | 0.487 | -0.8189 | 0.2258 | 3099863 | 3268446 |
| depth_bid_l5_avg_w10s_v1 | drift | 2.886 | 0.4713 | -0.8102 | 0.263 | 3099863 | 3268446 |
| depth_ask_l10_v1 | drift | 2.832 | 0.586 | -0.9402 | 0.2639 | 3100121 | 3269150 |
| depth_ask_l5_avg_w1s_v1 | drift | 2.648 | 0.4606 | -0.8031 | 0.2383 | 3098828 | 3264710 |
| depth_bid_l5_avg_w1s_v1 | drift | 2.632 | 0.4448 | -0.7981 | 0.2765 | 3098828 | 3264710 |
| depth_bid_l5_v1 | drift | 2.516 | 0.4294 | -0.7886 | 0.2849 | 3100121 | 3269150 |
| depth_ask_l5_v1 | drift | 2.513 | 0.4388 | -0.7925 | 0.2482 | 3100121 | 3269150 |
| depth_bid_l1_avg_w10s_v1 | drift | 2.421 | 0.4274 | -0.6998 | 0.267 | 3099863 | 3268446 |
| depth_ask_l1_avg_w10s_v1 | drift | 2.343 | 0.432 | -0.7499 | 0.2551 | 3099863 | 3268446 |
| depth_bid_l1_avg_w1s_v1 | drift | 1.731 | 0.3793 | -0.5782 | 0.302 | 3098828 | 3264710 |
| avg_order_size_ask_l1_v1 | drift | 1.696 | 0.5561 | -0.4602 | 0.4491 | 3100121 | 3269150 |
| depth_ask_l1_avg_w1s_v1 | drift | 1.674 | 0.3882 | -0.6133 | 0.2688 | 3098828 | 3264710 |
| depth_total_l1_v1 | drift | 1.622 | 0.428 | -0.6302 | 0.3133 | 3100121 | 3269150 |
| participation_w1m_v1 | drift | 1.622 | 0.5357 | 1.264 | 1.079 | 3100110 | 3269146 |
| avg_order_size_bid_l1_v1 | drift | 1.587 | 0.5352 | -0.4153 | 0.4202 | 3100121 | 3269150 |
| trade_count_w1m_v1 | drift | 1.57 | 0.4941 | 1.788 | 2.883 | 3100160 | 3269182 |
| trade_intensity_w1m_v1 | drift | 1.57 | 0.4941 | 1.788 | 2.883 | 3100160 | 3269182 |
| participation_w10s_v1 | drift | 1.306 | 0.4771 | 1.513 | 1.648 | 3099863 | 3268446 |
| depth_ask_l1_v1 | drift | 1.273 | 0.3918 | -0.505 | 0.3174 | 3100121 | 3269150 |
| execute_intensity_w1m_v1 | drift | 1.174 | 0.4293 | 1.738 | 3.021 | 3100160 | 3269182 |
| add_intensity_w1m_v1 | drift | 1.154 | 0.4422 | 5.072 | 7.782 | 3100160 | 3269182 |
| depth_bid_l1_v1 | drift | 1.143 | 0.4073 | -0.489 | 0.3395 | 3100121 | 3269150 |
| cancel_intensity_w1m_v1 | drift | 1.141 | 0.4403 | 5.735 | 8.825 | 3100160 | 3269182 |
| add_intensity_w10s_v1 | drift | 1.088 | 0.4348 | 3.725 | 5.961 | 3100160 | 3269182 |
| cancel_intensity_w10s_v1 | drift | 1.071 | 0.4332 | 5.184 | 8.226 | 3100160 | 3269182 |
| trade_count_w10s_v1 | drift | 1.006 | 0.4203 | 1.262 | 2.61 | 3100160 | 3269182 |
| trade_intensity_w10s_v1 | drift | 1.006 | 0.4203 | 1.262 | 2.61 | 3100160 | 3269182 |
| add_intensity_w1s_v1 | drift | 0.9698 | 0.3983 | 2.36 | 4.242 | 3100160 | 3269182 |
| cancel_intensity_w1s_v1 | drift | 0.9377 | 0.3969 | 3.885 | 6.81 | 3100160 | 3269182 |
| jump_count_w5m_v1 | drift | 0.8545 | 0.4045 | 2.265 | 8.18 | 3100160 | 3269182 |
| execute_intensity_w10s_v1 | drift | 0.7913 | 0.3708 | 1.184 | 2.535 | 3100160 | 3269182 |
| ofi_l10_w30s_v1 | drift | 0.5617 | 0.1482 | 0.0002826 | 0.5629 | 3100160 | 3269182 |
| ofi_l3_w30s_v1 | drift | 0.5578 | 0.1378 | 0.00153 | 0.3974 | 3100160 | 3269182 |
| ofi_l1_w30s_v1 | drift | 0.5519 | 0.1389 | 0.001117 | 0.3512 | 3100160 | 3269182 |
| ofi_l1_w5s_v1 | drift | 0.5412 | 0.1399 | 0.001315 | 0.3625 | 3100160 | 3269182 |
| jump_flag_w1m_v1 | drift | 0.5198 | 0.2508 | 1.132 | 2.073 | 2989239 | 3194421 |
| norm_volume_m5_v1 | drift | 0.4903 | 0.2928 | 0.0587 | 2.578 | 3092052 | 3236079 |
| mid_change_ticks_30s_v1 | drift | 0.4869 | 0.1574 | -0.009716 | 10.68 | 3099696 | 3266358 |
