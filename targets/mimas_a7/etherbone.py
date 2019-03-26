#!/usr/bin/env python3

from migen import *
from migen.genlib.resetsync import AsyncResetSynchronizer
import argparse
from litex.soc.integration.soc_core import *
from litex.soc.integration.soc_sdram import *
from litex.soc.integration.builder import *
from litex.soc.cores.uart import *


from litedram.modules import MT41K128M16
from litedram.phy import a7ddrphy
from litedram.core import ControllerSettings

from utils import csr_map_update, period_ns

from mimas_a7 import Platform

from litex.soc.cores.uart import UARTWishboneBridge
from litescope import LiteScopeIO, LiteScopeAnalyzer


from liteeth.common import convert_ip
from liteeth.phy.s7rgmii import LiteEthPHYRGMII
from liteeth.core import LiteEthUDPIPCore
from liteeth.frontend.etherbone import LiteEthEtherbone


class CRG(Module):
    def __init__(self, platform):
        self.clock_domains.cd_sys = ClockDomain()
        self.clock_domains.cd_clk200 = ClockDomain()
        self.clock_domains.cd_clk100 = ClockDomain()

        clk100 = platform.request("clk100")
        rst = platform.request("cpu_reset")

        pll_locked = Signal()
        pll_fb = Signal()
        self.pll_sys = Signal()
        pll_clk200 = Signal()
        self.specials += [
            Instance("PLLE2_BASE",
                     p_STARTUP_WAIT="FALSE", o_LOCKED=pll_locked,

                     # VCO @ 1400 MHz
                     p_REF_JITTER1=0.01, p_CLKIN1_PERIOD=10.0,
                     p_CLKFBOUT_MULT=14, p_DIVCLK_DIVIDE=1,
                     i_CLKIN1=clk100, i_CLKFBIN=pll_fb, o_CLKFBOUT=pll_fb,

                     # 140 MHz
                     p_CLKOUT0_DIVIDE=10, p_CLKOUT0_PHASE=0.0,
                     o_CLKOUT0=self.pll_sys,

                     # 200 MHz
                     p_CLKOUT1_DIVIDE=7, p_CLKOUT1_PHASE=0.0,
                     o_CLKOUT1=pll_clk200,
            ),
            Instance("BUFG", i_I=self.pll_sys, o_O=self.cd_sys.clk),
            Instance("BUFG", i_I=pll_clk200, o_O=self.cd_clk200.clk),
            Instance("BUFG", i_I=clk100, o_O=self.cd_clk100.clk),
            AsyncResetSynchronizer(self.cd_sys, ~pll_locked | rst),
            AsyncResetSynchronizer(self.cd_clk200, ~pll_locked | rst),
            AsyncResetSynchronizer(self.cd_clk100, ~pll_locked | rst),
        ]

        reset_counter = Signal(4, reset=15)
        ic_reset = Signal(reset=1)
        self.sync.clk200 += \
            If(reset_counter != 0,
                reset_counter.eq(reset_counter - 1)
            ).Else(
                ic_reset.eq(0)
            )
        self.specials += Instance("IDELAYCTRL", i_REFCLK=ClockSignal("clk200"), i_RST=ic_reset)


class EtherboneSoC(SoCCore):
    csr_peripherals = {
        "ethphy",
        "ethcore",
        "io",
        "analyzer"
    }
    csr_map_update(SoCCore.csr_map, csr_peripherals)

    def __init__(self, platform, mac_address=0x10e2d5000000, ip_address="192.168.100.50"):
        clk_freq = int(142e6)
        SoCCore.__init__(self, platform, clk_freq,
            cpu_type=None,
            integrated_main_ram_size=0x8000,
            integrated_rom_size=0x8000,
            integrated_sram_size=0x8000,
            csr_data_width=32,
            with_uart=False)

        self.submodules.crg = CRG(platform)

        self.crg.cd_sys.clk.attr.add("keep")
        self.platform.add_period_constraint(self.crg.cd_sys.clk, period_ns(clk_freq))

        # ethernet mac/udp/ip stack
        self.submodules.ethphy = LiteEthPHYRGMII(self.platform.request("eth_clocks"),
                                                 self.platform.request("eth"))
        self.submodules.ethcore = LiteEthUDPIPCore(self.ethphy,
                                                   mac_address,
                                                   convert_ip(ip_address),
                                                   self.clk_freq,
                                                   with_icmp=True)

        # etherbone bridge
        self.add_cpu_or_bridge(LiteEthEtherbone(self.ethcore.udp, 1234))
        self.add_wb_master(self.cpu_or_bridge.wishbone.bus)

        self.submodules.uartwishbone = UARTWishboneBridge(platform.request("serial"),
                                                          clk_freq, baudrate=115200)
        self.add_wb_master(self.uartwishbone.wishbone)

        # Litescope IO
        self.submodules.io = LiteScopeIO(8)
        for i in range(8):
            try:
                self.comb += platform.request("user_led", i).eq(self.io.output[i])
            except:
                pass

            # Litescope Analyzer
        analyzer_groups = {}

        # counter group
        counter = Signal(16, name_override="counter")
        zero = Signal(name_override="zero")
        self.sync += counter.eq(counter + 1)
        self.comb += zero.eq(counter == 0)
        analyzer_groups[0] = [
            zero,
            counter
        ]

        # communication group
        analyzer_groups[1] = [
            platform.lookup_request("serial").tx,
            platform.lookup_request("serial").rx,
            self.cpu_or_bridge.wishbone.bus,
            self.uartwishbone.wishbone,
        ]

        # fsm group
        fsm = FSM(reset_state="STATE1")
        self.submodules += fsm
        fsm.act("STATE1",
                NextState("STATE2")
                )
        fsm.act("STATE2",
                NextState("STATE1")
                )
        analyzer_groups[2] = [
            fsm
        ]

        analyzer_groups[3] = [
            self.ethphy.tx.sink,
            self.ethphy.rx.source
        ]

        # analyzer
        self.submodules.analyzer = LiteScopeAnalyzer(analyzer_groups, 512)

        self.ethphy.crg.cd_eth_rx.clk.attr.add("keep")
        self.ethphy.crg.cd_eth_tx.clk.attr.add("keep")
        self.platform.add_period_constraint(self.ethphy.crg.cd_eth_rx.clk, period_ns(125e6))
        self.platform.add_period_constraint(self.ethphy.crg.cd_eth_tx.clk, period_ns(125e6))

        self.platform.add_false_path_constraints(
            self.crg.cd_sys.clk,
            self.ethphy.crg.cd_eth_rx.clk,
            self.ethphy.crg.cd_eth_tx.clk)

    def do_exit(self, vns):
        self.analyzer.export_csv(vns, "test/analyzer.csv")


def main():
    parser = argparse.ArgumentParser(description="Nexys LiteX SoC")
    builder_args(parser)
    soc_sdram_args(parser)
    parser.add_argument("--nocompile-gateware", action="store_true")
    args = parser.parse_args()

    platform = Platform()
    soc = EtherboneSoC(platform)
    builder = Builder(soc, output_dir="build",
                      compile_gateware=not args.nocompile_gateware,
                      csr_csv="test/csr.csv")
    vns = builder.build()
    soc.analyzer.export_csv(vns, "test/analyzer.csv")

if __name__ == "__main__":
    main()

