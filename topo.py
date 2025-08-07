from mininet.topo import Topo
from mininet.net import Mininet
from mininet.node import RemoteController
from mininet.link import TCLink
from mininet.cli import CLI
from mininet.log import setLogLevel, info

k = 6  # 总 middle 数量（前4为TCP，后2为UDP）

def dpid_from_bytes(b1, b2, b3):
    """构造符合规范的 DPID: 00:00:00:00:00:b1:b2:b3"""
    return f"{0:02x}"*5 + f"{b1:02x}{b2:02x}{b3:02x}"


class FatTreeTopo(Topo):
    def build(self, k):
        # === Edge Switches ===
        Edge1 = []
        Edge2 = []
        for e1 in range(1):
            sw = self.addSwitch(name=f"edge_0_{e1}", dpid=dpid_from_bytes(1, 1, e1+1))
            Edge1.append(sw)
        for e2 in range(1):
            sw = self.addSwitch(name=f"edge_1_{e2}", dpid=dpid_from_bytes(1, 2, e2+1))
            Edge2.append(sw)

        # === Middle Switches ===
        Middle = []
        for m in range(k):
            sw = self.addSwitch(name=f"middle{m}", dpid=dpid_from_bytes(2, m, 0))
            Middle.append(sw)

        # === Edge ↔ Middle connections with QoS ===

        # TCP: middle0~3 with uniform QoS
        for i in range(4):
            # Edge1 → Middle[i]
            self.addLink(Edge1[0], Middle[i], port1=i + k + 1, port2=1, cls=TCLink,
                         bw=40, delay='10ms', max_queue_size=100)
            # Middle[i] → Edge2
            self.addLink(Middle[i], Edge2[0], port1=2, port2=i + k + 1, cls=TCLink,
                         bw=40, delay='10ms', max_queue_size=100)

        # UDP 主路径: middle4（高QoS）
        self.addLink(Edge1[0], Middle[4], port1=4 + k + 1, port2=1, cls=TCLink,
                     bw=100, delay='1ms', max_queue_size=100)
        self.addLink(Middle[4], Edge2[0], port1=2, port2=4 + k + 1, cls=TCLink,
                     bw=100, delay='1ms', max_queue_size=100)

        # UDP 备用路径: middle5（较低QoS）
        self.addLink(Edge1[0], Middle[5], port1=5 + k + 1, port2=1, cls=TCLink,
                     bw=20, delay='10ms', max_queue_size=100)
        self.addLink(Middle[5], Edge2[0], port1=2, port2=5 + k + 1, cls=TCLink,
                     bw=20, delay='10ms', max_queue_size=100)

        # === Hosts & Edge ↔ Host connection ===
        for h1 in range(k):
            ip = f"10.1.1.{h1 + 2}"
            hostname = f"h_1_1_{h1 + 1}"
            host = self.addHost(hostname, ip=ip)
            port = h1 + 1
            self.addLink(host, Edge1[0], port2=port)

        for h2 in range(k):
            ip = f"10.2.1.{h2 + 2}"
            hostname = f"h_2_1_{h2 + 1}"
            host = self.addHost(hostname, ip=ip)
            port = h2 + 1
            self.addLink(host, Edge2[0], port2=port)


def run(k):
    topo = FatTreeTopo(k=k)
    net = Mininet(topo=topo,
                  link=TCLink,
                  controller=None,
                  autoSetMacs=True,
                  autoStaticArp=True)
    net.controllers = []
    net.addController('c0',
                      controller=RemoteController,
                      ip='127.0.0.1',
                      port=6633,
                      protocols="OpenFlow13")
    net.start()
    CLI(net)
    net.stop()

def run_iperf(net):
    for i in range(1, 7):
        client = net.get(f'h_1_1_{i}')
        server = net.get(f'h_2_1_{i}')
        server.cmd('iperf -s -u -i 1 > /tmp/iperf_s_%d.log &' % i)
        client.cmd('iperf -c %s -u -b 20M -t 10 > /tmp/iperf_c_%d.log &' % (server.IP(), i))



if __name__ == '__main__':
    setLogLevel('info')
    run(k)

