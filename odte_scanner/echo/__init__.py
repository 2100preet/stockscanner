"""TradeEcho-style desk: OptionFlow, DealerEdge (GEX), AlgoEdge, Pulse, Mirror.

Research proxies from Yahoo chains + our ensemble — not affiliated with Trade Echo.
True OPRA flow / ATS dark pool / dealer GEX feeds are not available on free Yahoo.
"""

from odte_scanner.echo.board import build_echo_board

__all__ = ["build_echo_board"]
