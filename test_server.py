import socket

HOST = "0.0.0.0"
PORT = 9000

s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
s.bind((HOST, PORT))
s.listen()
print(f"Listening on {PORT}...")

while True:
    conn, addr = s.accept()
    data = conn.recv(1024).decode("utf-8", "ignore").strip()
    print(f"[{addr[0]}] {data}")
    conn.sendall(b"OK\n")
    conn.close()