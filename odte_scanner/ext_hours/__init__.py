"""Extended-hours shadow desks (alerts only, never orders).

* ``ah_earnings``: S2 after-hours earnings drift. 5:00 PM CT Telegram alert,
  8:35 AM CT result, shadow ledger ``outputs/ah_earnings_shadow.json``.
* ``overnight_bias``: S1 context line at 2:55 PM CT when QQQ/IWM sit in the
  bottom 25% of the day range; close-to-open shadow ledger.

Nothing in this package imports ``odte_scanner.trading`` or any broker/router
module; it only reads quotes and sends Telegram text.
"""
SHADOW_LABEL = "SHADOW (alerts only, no order placed)"
