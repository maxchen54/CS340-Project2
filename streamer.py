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

                if type == 0: # Data packet, send ACK Back
                    # Check hash result
                    check_hash = hashlib.md5(payload).digest()

                    # Only buffer packet if hash matches. Otherwise do nothing  
                    if check_hash == hash:
                        # Include an empty hash for non-data packets
                        empty_hash = hashlib.md5(b'').digest()
                        ack_header = struct.pack(HEADER_FORMAT, 1, seq, empty_hash)
                        self.socket.sendto(ack_header, addr)

                        # Store data in buffer
                        with self.condval:
                            self.recv_buffer[seq] = payload
                            self.condval.notify_all()

                elif type == 1: # ACK packet, send nothing
                    with self.lock:
                        self.ack = True

                elif type == 2: # FIN packet, send FIN-ACK back
                    empty_hash = hashlib.md5(b'').digest()
                    fin_ack_header = struct.pack(HEADER_FORMAT, 3, seq, empty_hash)
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
            hash = hashlib.md5(payload).digest()
            header = struct.pack(HEADER_FORMAT, 0, self.send_seq, hash) # 0 indicate this packet is data, not ack
            packet = header + payload

            # Timeout for waiting for ACKs
            self.ack = False 
            timeout = 0.25
            start_time = time.time()
            self.socket.sendto(packet, (self.dst_ip, self.dst_port)) 

            # Wait for ACK from the send above
            while not self.ack:
                if time.time() - start_time > timeout: 
                    # Exceed timeout, resend packet (retransmission)
                    self.socket.sendto(packet, (self.dst_ip, self.dst_port))
                    start_time = time.time() # Reset Timer
                time.sleep(0.01)
            
            self.send_seq += 1

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
        # Send a FIN packet
        self.fin_ack = False
        empty_hash = hashlib.md5(b'').digest()
        fin_header = struct.pack(HEADER_FORMAT, 2, self.send_seq, empty_hash)
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
