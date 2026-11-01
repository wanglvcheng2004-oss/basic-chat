"""Single-process launcher with a loopback management entry in Radmin mode."""
import argparse
import asyncio
import ipaddress
import socket

import uvicorn


def addresses(value):
    address = ipaddress.ip_address(value)
    if address.version != 4 or address.is_unspecified or address.is_multicast:
        raise ValueError('请指定本机的具体 IPv4 地址。')
    return list(dict.fromkeys(['127.0.0.1', str(address)]))


def main():
    parser = argparse.ArgumentParser(description='本机或 Radmin 单进程服务')
    parser.add_argument('--listen', default='127.0.0.1')
    parser.add_argument('--port', type=int, default=8000)
    args = parser.parse_args()
    if not 1024 <= args.port <= 65535:
        parser.error('端口应介于 1024 与 65535 之间。')
    try:
        listen = addresses(args.listen)
    except ValueError as exc:
        parser.error(str(exc))
    sockets = []
    try:
        for address in listen:
            endpoint = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
            sockets.append(endpoint)
            if hasattr(socket, 'SO_EXCLUSIVEADDRUSE'):
                endpoint.setsockopt(socket.SOL_SOCKET, socket.SO_EXCLUSIVEADDRUSE, 1)
            endpoint.bind((address, args.port))
            endpoint.listen(128)
            endpoint.setblocking(False)
            print(f'Chat URL: http://{address}:{args.port}', flush=True)
        config = uvicorn.Config('backend.main:app', workers=1, proxy_headers=False, ws_max_size=16384)
        asyncio.run(uvicorn.Server(config).serve(sockets=sockets))
    finally:
        for endpoint in sockets:
            endpoint.close()


if __name__ == '__main__':
    main()
