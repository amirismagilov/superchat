"""Temporary-route tests use fake sockets; no external endpoints are contacted."""
import base64
import contextlib
import errno
import hashlib
import importlib.util
import io
import json
from pathlib import Path
import socket
import ssl
import time
import unittest
from unittest.mock import patch

ROOT=Path(__file__).resolve().parents[1]
spec=importlib.util.spec_from_file_location('mac_route_probe',ROOT/'scripts/probe_messenger_via_mac.py')
diag=importlib.util.module_from_spec(spec);spec.loader.exec_module(diag)


class FakeSocket:
    def __init__(self, data=b'', chunk=1):self.data=bytearray(data);self.chunk=chunk;self.sent=[];self.timeouts=[];self.closed=False;self.address=None
    def recv(self,size):
        size=min(size,self.chunk,len(self.data));data=bytes(self.data[:size]);del self.data[:size];return data
    def sendall(self,data):self.sent.append(data)
    def settimeout(self,value):self.timeouts.append(value)
    def connect(self,address):self.address=address
    def close(self):self.closed=True


class MacRouteTests(unittest.TestCase):
    def test_socks_handles_fragmented_reply_and_remote_dns_without_auth(self):
        sock=FakeSocket(b'\x05\x00\x05\x00\x00\x03\x04bind\x01\xbb')
        diag.socks_handshake(sock,diag.WA_HOST,time.monotonic()+1)
        self.assertEqual(sock.sent[0],b'\x05\x01\x00')
        self.assertEqual(sock.sent[1],b'\x05\x01\x00\x03'+bytes([len(diag.WA_HOST)])+diag.WA_HOST.encode()+b'\x01\xbb')
        self.assertEqual(sock.data,b'');self.assertTrue(all(0<t<=1 for t in sock.timeouts))

    def test_socks_ipv4_and_ipv6_bound_reply_are_fully_consumed(self):
        for atyp, address in [(1,b'\x7f\x00\x00\x01'),(4,b'\0'*16)]:
            sock=FakeSocket(b'\x05\x00'+bytes([5,0,0,atyp])+address+b'\x01\xbb')
            diag.socks_handshake(sock,diag.TG_HOST,time.monotonic()+1)
            self.assertEqual(sock.sent[1],b'\x05\x01\x00\x01'+socket.inet_aton(diag.TG_HOST)+b'\x01\xbb')
            self.assertEqual(sock.data,b'')

    def test_socks_denials_truncation_and_unknown_endpoint_fail_closed(self):
        for reply, expected in [(b'\x05\x02','socks_authentication_unavailable'),(b'\x05\x00\x05\x02\x00\x01','socks_connection_not_allowed'),(b'\x05\x00\x05\x00\x00\x7f','socks_protocol_error'),(b'\x05','peer_closed')]:
            with self.assertRaises(diag.ProbeError) as raised:diag.socks_handshake(FakeSocket(reply),'web.whatsapp.com',time.monotonic()+1)
            self.assertEqual(raised.exception.status,expected)
        sock=FakeSocket()
        with self.assertRaises(diag.ProbeError):diag.socks_handshake(sock,'SECRET_UNAPPROVED_HOST',time.monotonic()+1)
        self.assertEqual(sock.sent,[])

    def ws_reply(self, **changes):
        key=base64.b64encode(b'k'*16).decode()
        accept=base64.b64encode(hashlib.sha1((key+diag.GUID).encode()).digest()).decode()
        values={'status':'101 Switching Protocols','upgrade':'websocket','connection':'keep-alive, Upgrade','accept':accept}
        values.update(changes)
        return ('HTTP/1.1 '+values['status']+'\r\nUpgrade: '+values['upgrade']+'\r\nConnection: '+values['connection']+'\r\nSec-WebSocket-Accept: '+values['accept']+'\r\nSet-Cookie: SECRET_COOKIE\r\nSet-Cookie: SECRET_OTHER_COOKIE\r\n\r\n').encode()

    def test_websocket_validates_rfc_accept_origin_host_and_sends_no_frames(self):
        sock=FakeSocket(self.ws_reply(),chunk=3)
        with patch.object(diag.os,'urandom',return_value=b'k'*16):diag.websocket_upgrade(sock,time.monotonic()+1)
        self.assertEqual(len(sock.sent),1)
        request=sock.sent[0]
        self.assertTrue(request.startswith(b'GET /ws/chat HTTP/1.1\r\n'))
        self.assertIn(b'Host: web.whatsapp.com\r\n',request)
        self.assertIn(b'Origin: https://web.whatsapp.com\r\n',request)
        self.assertNotIn(b'Authorization',request);self.assertNotIn(b'Cookie:',request)

    def test_websocket_rejects_redirect_bad_accept_and_oversized_headers(self):
        for response, expected in [(self.ws_reply(status='302 SECRET_REDIRECT'),'http_rejected'),(self.ws_reply(accept='SECRET_BAD_ACCEPT'),'websocket_accept_invalid'),(self.ws_reply(upgrade='SECRET_UPGRADE'),'websocket_upgrade_invalid'),(b'HTTP/1.1 101 OK\r\nX-Secret: '+b'x'*5000,'http_headers_too_large')]:
            sock=FakeSocket(response,chunk=128)
            with patch.object(diag.os,'urandom',return_value=b'k'*16),self.assertRaises(diag.ProbeError) as raised:diag.websocket_upgrade(sock,time.monotonic()+1)
            self.assertEqual(raised.exception.status,expected);self.assertEqual(len(sock.sent),1)

    def test_tls_is_verified_for_real_hostname_and_all_sockets_close(self):
        plain=FakeSocket();tls=FakeSocket(self.ws_reply(),chunk=128);calls=[]
        class Context:
            def wrap_socket(self,sock,server_hostname):calls.append((sock,server_hostname));return tls
        with patch.object(diag,'resolve_whatsapp_ipv4',return_value=('127.0.0.2',443)),patch.object(diag.socket,'socket',return_value=plain),patch.object(diag.ssl,'create_default_context',return_value=Context()),patch.object(diag.os,'urandom',return_value=b'k'*16):
            result=diag.probe('server_direct_ipv4','whatsapp_wss',time.monotonic()+5)
        self.assertEqual(result['status'],'websocket_open');self.assertTrue(result['tls_verified']);self.assertTrue(tls.closed)
        self.assertEqual(calls,[(plain,'web.whatsapp.com')])
        self.assertEqual(ssl.create_default_context().verify_mode,ssl.CERT_REQUIRED)
        self.assertTrue(ssl.create_default_context().check_hostname)

    def test_cert_failure_never_falls_back_or_prints_error(self):
        plain=FakeSocket()
        class Context:
            def wrap_socket(self,*args,**kwargs):raise ssl.SSLCertVerificationError('SECRET_CERT_ERROR')
        with patch.object(diag,'resolve_whatsapp_ipv4',return_value=('127.0.0.2',443)),patch.object(diag.socket,'socket',return_value=plain),patch.object(diag.ssl,'create_default_context',return_value=Context()):result=diag.probe('server_direct_ipv4','whatsapp_wss',time.monotonic()+5)
        self.assertEqual(result['status'],'tls_certificate_error');self.assertTrue(plain.closed);self.assertNotIn('SECRET',json.dumps(result))

    def test_socks_path_uses_only_loopback_and_telegram_sends_only_proxy_handshake(self):
        sock=FakeSocket(b'\x05\x00\x05\x00\x00\x01\0\0\0\0\0\0')
        with patch.object(diag.socket,'socket',return_value=sock),patch.object(diag.socket,'getaddrinfo',side_effect=AssertionError('DNS must occur on Mac')):
            result=diag.probe('via_mac_socks5','telegram_dc2_tcp',time.monotonic()+5)
        self.assertEqual(result['status'],'tcp_connected');self.assertEqual(sock.address,('127.0.0.1',19187));self.assertEqual(len(sock.sent),2);self.assertTrue(sock.closed)

    def test_stalled_dns_is_bounded_without_waiting_for_resolver_thread(self):
        def slow(*args):time.sleep(.3);return []
        started=time.monotonic()
        with patch.object(diag.socket,'getaddrinfo',side_effect=slow),self.assertRaises(TimeoutError):diag.resolve_whatsapp_ipv4(time.monotonic()+.03)
        self.assertLess(time.monotonic()-started,.2)

    def test_expired_deadline_prevents_sends_and_raw_os_errors_are_redacted(self):
        sock=FakeSocket()
        with self.assertRaises(TimeoutError):diag.read_exact(sock,2,time.monotonic()-1)
        self.assertEqual(sock.sent,[])
        with patch.object(diag.socket,'socket',side_effect=OSError(errno.ECONNREFUSED,'SECRET_PROXY_PASSWORD')):
            result=diag.probe('via_mac_socks5','telegram_dc2_tcp',time.monotonic()+5)
        self.assertEqual(result['status'],'connection_refused');self.assertNotIn('SECRET',json.dumps(result))

    def test_packet_only_probes_fixed_targets_three_wa_rounds_and_two_tcp_routes(self):
        calls=[]
        def probe(route,endpoint,deadline):calls.append((route,endpoint,deadline));return {'status':'websocket_open' if endpoint=='whatsapp_wss' else 'tcp_connected','elapsed_ms':1}
        with patch.object(diag,'probe',side_effect=probe):result=diag.diagnose()
        self.assertEqual(len(calls),8)
        self.assertEqual([endpoint for _,endpoint,_ in calls],['whatsapp_wss']*6+['telegram_dc2_tcp']*2)
        self.assertEqual(set(route for route,_,_ in calls),set(diag.ROUTES))
        self.assertEqual(len({deadline for _,_,deadline in calls}),1)
        self.assertTrue(all(item['successes']==3 for item in result['whatsapp'].values()))
        self.assertIn('not_account_authentication',result['scope'])

    def test_main_prints_no_exception_details_and_installs_hard_deadline(self):
        output=io.StringIO()
        with patch.object(diag,'diagnose',side_effect=RuntimeError('SECRET_EXCEPTION')),patch.object(diag.signal,'signal'),patch.object(diag.signal,'alarm') as alarm,contextlib.redirect_stdout(output):self.assertEqual(diag.main(),1)
        self.assertNotIn('SECRET',output.getvalue());self.assertEqual([args[0][0] for args in alarm.call_args_list],[65,0])


if __name__=='__main__':unittest.main()
