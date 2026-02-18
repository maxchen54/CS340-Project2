# do not import anything else from loss_socket besides LossyUDP
from lossy_socket import LossyUDP
# do not import anything else from socket except INADDR_ANY
from socket import INADDR_ANY

import struct
import concurrent.futures
import threading
import time
import hashlib

HEADER_FORMAT = "!BI16s" # B: 1 byte for packet type, I: 4 bytes for sequence number
# We will set 0=Data packet, 1=ACK packet, 2=FIN Packet, 3=FIN-ACK packet
HEADER_SIZE = struct.calcsize(HEADER_FORMAT)


class Streamer:
    def __init__(self, dst_ip, dst_port,
                 src_ip=INADDR_ANY, src_port=0):
        """Default values listen on all network interfaces, chooses a random source port,
           and does not introduce any simulated packet loss."""
        self.socket = LossyUDP()
        self.socket.bind((src_ip, src_port))
        self.dst_ip = dst_ip
        self.dst_port = dst_port
        # Seq for keeping order
        self.send_seq = 0
        self.expected_seq = 0
        self.recv_buffer = {}
        self.closed = False

        self.ack = True # Initialized to true so first packet can be sent
        self.fin = False 
        self.fin_ack = False

        self.send_buffer = {}
        self.base = 0
        self.timer = None 
        

        self.lock = threading.Lock()
        self.condval = threading.Condition(self.lock)

        self.executor = concurrent.futures.ThreadPoolExecutor(max_workers=1)
        self.executor.submit(self.listener)

    def listener(self):
        while not self.closed:
            try:
                data, addr = self.socket.recvfrom()  # Return a packet

                # Check if socket was closed (returns empty data)
                if len(data) < HEADER_SIZE:
                    continue

                header = data[:HEADER_SIZE]
                payload = data[HEADER_SIZE:]

                (type, seq, hash) = struct.unpack(HEADER_FORMAT, header)

                # Check for hash immediately, and ignore packet if it's corrupted
                check_hash = hashlib.md5(struct.pack("!BI", type, seq) + payload).digest()
                if check_hash != hash:
                    continue  

                if type == 0: # Data packet, send ACK Back
                    ack_hash = hashlib.md5(struct.pack("!BI", 1, seq) + b'').digest()
                    ack_header = struct.pack(HEADER_FORMAT, 1, seq, ack_hash)
                    self.socket.sendto(ack_header, addr)

                    # Store data in buffer
                    with self.condval:
                        self.recv_buffer[seq] = payload
                        self.condval.notify_all()

                elif type == 1: # ACK packet
                    with self.lock:
                        # Only remove the specific ACKed packet
                        if seq in self.send_buffer:
                            del self.send_buffer[seq]
                        while self.base not in self.send_buffer and self.base < self.send_seq:
                            self.base += 1
                        if self.send_buffer:
                            self.start_timer()
                        else:
                            if self.timer is not None:
                                self.timer.cancel()
                                self.timer = None

                elif type == 2: # FIN packet, send FIN-ACK back
                    fin_ack_hash = hashlib.md5(struct.pack("!BI", 3, seq) + b'').digest()
                    fin_ack_header = struct.pack(HEADER_FORMAT, 3, seq, fin_ack_hash)
                    self.socket.sendto(fin_ack_header, addr)
                    with self.lock: 
                        self.fin = True 

                elif type == 3: # FIN-ACK packet
                    with self.lock:
                        self.fin_ack = True

            except Exception as e:
                print("Listener died!")
                print(e)

    def send(self, data_bytes: bytes) -> None:
        """Note that data_bytes can be larger than one packet."""
        # for now I'm just sending the raw application-level data in one UDP payload
        for i in range(0, len(data_bytes), 1472 - HEADER_SIZE):
            offset = min(i + (1472 - HEADER_SIZE), len(data_bytes))
            payload = data_bytes[i:offset]
            hash = hashlib.md5(struct.pack("!BI", 0, self.send_seq) + payload).digest()
            header = struct.pack(HEADER_FORMAT, 0, self.send_seq, hash) # 0 indicate this packet is data, not ack
            packet = header + payload

            with self.lock:
                self.send_buffer[self.send_seq] = packet
                self.send_seq += 1

                # If it is the first unacked packet, start the timer
                if self.timer is None:
                    self.start_timer()

            # Send immediately
            self.socket.sendto(packet, (self.dst_ip, self.dst_port)) 

    def recv(self) -> bytes:
        """Blocks (waits) if no data is ready to be read from the connection."""        

        with self.condval:
            while self.expected_seq not in self.recv_buffer and not self.closed:
                self.condval.wait()

            if self.closed and self.expected_seq not in self.recv_buffer:
                return b""

            out = self.recv_buffer.pop(self.expected_seq)
            self.expected_seq += 1
            return out

    def close(self) -> None:
        """Cleans up. It should block (wait) until the Streamer is done with all
           the necessary ACKs and retransmissions"""
        # Make sure all our in-flight packets are ACKed
        while True: 
            with self.lock:
                if not self.send_buffer:
                    break
            time.sleep(0.01)

        # Send a FIN packet
        self.fin_ack = False
        fin_hash = hashlib.md5(struct.pack("!BI", 2, self.send_seq) + b'').digest()
        fin_header = struct.pack(HEADER_FORMAT, 2, self.send_seq, fin_hash)
        timeout = 0.25
        start_time = time.time()
        self.socket.sendto(fin_header, (self.dst_ip, self.dst_port))

        # Wait for FIN-ACK
        while not self.fin_ack:
            if time.time() - start_time > timeout:
                self.socket.sendto(fin_header, (self.dst_ip, self.dst_port))
                start_time = time.time()
            time.sleep(0.01)

        self.send_seq += 1

        # Wait for FIN from the other side, this is different than wait for FIN-ACK
        while not self.fin:
            time.sleep(0.01)
    
        time.sleep(2)
        
        # Stop the listener thread
        self.closed = True
        self.socket.stoprecv()

        with self.condval:
            self.condval.notify_all()

        self.executor.shutdown(wait=True)

    def start_timer(self):
        # Helper function to start the timer
        if self.timer is not None: 
            self.timer.cancel()
        self.timer = threading.Timer(0.25, self.retransmit)
        self.timer.daemon = True
        self.timer.start()

    def retransmit(self):
        # Retransmit all packets in send_buffer
        with self.lock:
            for seq in sorted(self.send_buffer.keys()):
                self.socket.sendto(self.send_buffer[seq], (self.dst_ip, self.dst_port))
            if self.send_buffer:
                self.start_timer()
            else:
                self.timer = None