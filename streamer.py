# do not import anything else from loss_socket besides LossyUDP
from lossy_socket import LossyUDP
# do not import anything else from socket except INADDR_ANY
from socket import INADDR_ANY

import struct

HEADER_FORMAT = "!I"
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

    def send(self, data_bytes: bytes) -> None:
        """Note that data_bytes can be larger than one packet."""
        # for now I'm just sending the raw application-level data in one UDP payload
        for i in range(0, len(data_bytes), 1472):
            offset = min(i+1472, len(data_bytes))
            payload = data_bytes[i:offset]
            header = struct.pack(HEADER_FORMAT, self.send_seq)
            packet = header + payload

            self.socket.sendto(packet, (self.dst_ip, self.dst_port))

            self.send_seq += 1 

    def recv(self) -> bytes:
        """Blocks (waits) if no data is ready to be read from the connection."""        
        # this sample code just calls the recvfrom method on the LossySocket
        
        while True:
            data, addr = self.socket.recvfrom() # Return a packet
            
            header = data[:HEADER_SIZE]
            payload = data[HEADER_SIZE:]
            
            (seq,) = struct.unpack(HEADER_FORMAT, header) 
            
            self.recv_buffer[seq] = payload

            if self.expected_seq in self.recv_buffer:
                out = self.recv_buffer.pop(self.expected_seq) # out is the payload
                self.expected_seq += 1
                return out
        
            



    def close(self) -> None:
        """Cleans up. It should block (wait) until the Streamer is done with all
           the necessary ACKs and retransmissions"""
        # your code goes here, especially after you add ACKs and retransmissions.
        pass
