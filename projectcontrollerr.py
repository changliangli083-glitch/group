from ryu.base import app_manager
from ryu.controller import ofp_event
from ryu.controller.handler import CONFIG_DISPATCHER, MAIN_DISPATCHER, DEAD_DISPATCHER
from ryu.controller.handler import set_ev_cls
from ryu.ofproto import ofproto_v1_3
from ryu.lib.packet import packet, ethernet, ipv4
from ryu.lib import hub
from collections import defaultdict

class Controller(app_manager.RyuApp):
    OFP_VERSIONS = [ofproto_v1_3.OFP_VERSION]
    k = 6
    ed = 1

    def __init__(self, *args, **kwargs):
        super(Controller, self).__init__(*args, **kwargs)
        self.k = Controller.k
        self.ed = Controller.ed

        # === 新增监测和状态标记 ===
        self.datapaths = {}
        self.ecmp_triggered = False
        self.monitor_thread = hub.spawn(self._monitor)

    # === 用于记录交换机连接状态 ===
    @set_ev_cls(ofp_event.EventOFPStateChange, [MAIN_DISPATCHER, DEAD_DISPATCHER])
    def _state_change_handler(self, ev):
        dp = ev.datapath
        if ev.state == MAIN_DISPATCHER:
            self.datapaths[dp.id] = dp
        elif ev.state == DEAD_DISPATCHER and dp.id in self.datapaths:
            del self.datapaths[dp.id]

    def _monitor(self):
        while True:
            for dp in self.datapaths.values():
                sw_type, *_ = self.classify_switch(dp.id)
                if sw_type == 'middle':
                    parser = dp.ofproto_parser
                    req = parser.OFPPortStatsRequest(dp, 0, dp.ofproto.OFPP_ANY)
                    dp.send_msg(req)
                    self.logger.info(f"[polling] Requesting Port Stats from {dp.id:016x}")
            hub.sleep(2)

    @set_ev_cls(ofp_event.EventOFPPortStatsReply, MAIN_DISPATCHER)
    def _port_stats_reply_handler(self, ev):
        dp = ev.msg.datapath
        dpid = dp.id
        sw_type, *_ = self.classify_switch(dpid)

        if sw_type != 'middle' or self.ecmp_triggered:
            return  # 仅中间交换机 + 仅首次触发

        for stat in ev.msg.body:
            # 忽略内部端口和非物理端口
            if stat.port_no == dp.ofproto.OFPP_LOCAL or stat.port_no > 20:
                continue

            tx_mb = stat.tx_bytes / 1024 / 1024
            rx_mb = stat.rx_bytes / 1024 / 1024
            total_mb = tx_mb + rx_mb

            self.logger.info(f"[Flow] middle:{dpid:016x} port:{stat.port_no} → {total_mb:.2f} MB")

            # 如果超过阈值触发一次 ECMP
            if total_mb > 100:
                self.logger.warning(f"[Trigger] High traffic ({total_mb:.2f} MB) detected at {dpid:016x}")
                self.ecmp_triggered = True
                self.install_ecmp_flows()
                break

    def install_ecmp_flows(self):
        dp = self.get_edge1_dp()
        if not dp:
            self.logger.error("edge1 disconnected")
            return

        parser = dp.ofproto_parser
        ofp = dp.ofproto
        for h in range(self.k):
            port = h + 1
            ip_dst = f"10.2.1.{h + 2}"
            match = parser.OFPMatch(eth_type=0x0800, ipv4_dst=ip_dst)
            actions = [parser.OFPActionOutput(port)]  # TCP 4条路径
            inst = [parser.OFPInstructionActions(ofp.OFPIT_APPLY_ACTIONS, actions)]
            mod = parser.OFPFlowMod(datapath=dp,
                                    priority=100,
                                    match=match,
                                    instructions=inst)
            dp.send_msg(mod)
            self.logger.info(f"Flow Entry was sent: {ip_dst} → ports {self.k + 1}-{self.k + 4}")

    def get_edge1_dp(self):
        for dp in self.datapaths.values():
            if self.classify_switch(dp.id)[0] == 'edge1':
                return dp
        return None

    # ============== 你的原始逻辑部分（不动） ==============

    @set_ev_cls(ofp_event.EventOFPSwitchFeatures, CONFIG_DISPATCHER)
    def switch_features_handler(self, ev):
        dp = ev.msg.datapath
        dpid = dp.id
        parser = dp.ofproto_parser

        sw_type, pod, idx, i, j = self.classify_switch(dpid)
        self.logger.info("✅ Switch %016x (%s) connected." % (dpid, sw_type))

        if sw_type == 'middle':
            self.install_middle_flows(dp, parser)
        elif sw_type == 'edge1':
            self.install_client_flows(dp, parser)
        elif sw_type == 'edge2':
            self.install_server_flows(dp, parser)

    def classify_switch(self, dpid):
        dpid_hex = "%016x" % dpid
        b1 = int(dpid_hex[-6:-4], 16)
        b2 = int(dpid_hex[-4:-2], 16)
        b3 = int(dpid_hex[-2:], 16)
        if b1 == 2:
            return 'middle', None, None, b2, b3
        elif b1 == 1 and b2 == 1:
            return 'edge1', b1, b2, None, None
        elif b1 == 1 and b2 == 2:
            return 'edge2', b1, b2, None, None

    def add_flow(self, dp, priority, match, actions):
        parser = dp.ofproto_parser
        ofp = dp.ofproto
        inst = [parser.OFPInstructionActions(ofp.OFPIT_APPLY_ACTIONS, actions)]
        mod = parser.OFPFlowMod(datapath=dp,
                                priority=priority,
                                match=match,
                                instructions=inst)
        dp.send_msg(mod)

    def install_middle_flows(self, dp, parser):
        ofp = dp.ofproto
        for ip2 in range(1, 3):
            out_port = ip2
            for ip3 in range(self.ed):
                for ip4 in range(self.k):
                    ip_dst = "10.%d.%d.%d" % (ip2, ip3 + 1, ip4 + 2)
                    match = parser.OFPMatch(eth_type=0x0800, ipv4_dst=ip_dst)
                    actions = [parser.OFPActionOutput(out_port)]
                    self.add_flow(dp, 10, match, actions)

    def install_client_flows(self, dp, parser):
        ofp = dp.ofproto
        for h in range(self.k):
            ip_dst = "10.1.1.%d" % (h + 2)
            match = parser.OFPMatch(eth_type=0x0800, ipv4_dst=ip_dst)
            actions = [parser.OFPActionOutput(h + 1)]
            self.add_flow(dp, 10, match, actions)

        for e in range(self.ed):
            for h in range(self.k):
                ip_dst = "10.2.%d.%d" % (e + 1, h + 2)
                match = parser.OFPMatch(eth_type=0x0800, ipv4_dst=ip_dst)
                actions = [parser.OFPActionOutput(self.k + 1)]
                self.add_flow(dp, 10, match, actions)

    def install_server_flows(self, dp, parser):
        ofp = dp.ofproto
        for h in range(self.k):
            ip_dst = "10.2.1.%d" % (h + 2)
            match = parser.OFPMatch(eth_type=0x0800, ipv4_dst=ip_dst)
            actions = [parser.OFPActionOutput(h + 1)]
            self.add_flow(dp, 100, match, actions)

        for e in range(self.ed):
            for h in range(self.k):
                ip_dst = "10.1.%d.%d" % (e + 1, h + 2)
                match = parser.OFPMatch(eth_type=0x0800, ipv4_dst=ip_dst)
                actions = [parser.OFPActionOutput(self.k + 1)]
                self.add_flow(dp, 10, match, actions)



