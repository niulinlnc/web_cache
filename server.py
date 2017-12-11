#!/usr/bin/env python
# -*- coding: utf-8 -*-

from datetime import datetime, timedelta
import hashlib
import socket
import threading

from storage import Storage


GMT_FORMAT = '%a, %d %b %Y %H:%M:%S GMT'

class Server:

    def __init__(self, host, port):
        self.host = host
        self.port = port
        self.init_storage('0.0.0.0', 6379)
        self.init_listen_socket()
        self.init_handle_method()

    def init_storage(self, *storage_addr):
        self.storage = Storage(*storage_addr)

    def init_listen_socket(self):
        # 创建一个ipv4的基于tcp的监听socket，server一被关闭，port就被回收
        self.listen_socket = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        self.listen_socket.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        # 绑定ip
        self.listen_socket.bind((self.host, self.port))
        # 监听
        self.listen_socket.listen(5)

    def init_handle_method(self):
        self.handle_method = {
            b'GET': self.do_get,
            b'POST': self.do_post,
            b'HEAD': self.do_head,
            b'DELETE': self.do_delete,
            b'PUT': self.do_put
        }

    # ========== end init ops =====================================================================

    # ========== start run ops =====================================================================
    def run(self):
        print('server running at {}:{} ...'.format(self.host, self.port))
        while True:
            # 等待连接
            client_socket, client_addr = self.listen_socket.accept()
            print('accept connection from {}'.format(client_addr))
            task = threading.Thread(target=self.handle_request, args=(client_socket, client_addr))
            task.start()

    # ========== start handle request ==============================================================
    def handle_request(self, client_socket, client_addr):
        # 该http请求报文是encode的
        request_msg = client_socket.recv(1024)
        print('\r\nrequest_msg: ', request_msg)             # =========================================== * 输出查看 * ==
        request_header, request_body = request_msg.split(b'\r\n\r\n', 1)
        request_line, header_line = request_header.split(b'\r\n', 1)
        method, url, http_version = request_line.split()
        self.handle_method[method](url, header_line, request_body, client_socket)

    # ========== start do requests ===========================================================
    def do_get(self, *args):
        status_code = '404'         # 默认为404，对代码重构时以下设为nonlocal
        url, header_line, _, client_sock = args
        url_hash = self.get_hash(url)

        # 首先获取对象初始服务器的host, ip, addr
        host, ip, port = self.get_host_addr(header_line)

        # 在web cache数据库中查找，是否存在hash表中
        if self.storage.exists(url_hash):
            saved_date, last_modified_date, response_body = \
                self.storage.mget(url_hash, b'saved_date', b'last_modified_date', b'entity_body')
            # 过期检查，有效时间设为一周
            cur_date = datetime.utcnow()
            saved_date = datetime.strptime(saved_date.decode('utf-8'), GMT_FORMAT)
            # 1) 如果还在有效期，直接返回
            if cur_date < (saved_date + timedelta(7)):
                status_code = '200'
                self.do_get_response(response_body, status_code, client_sock)       # 封装成响应报文发送给client
            # 2) 若超过有效期，则使用条件GET请求
            else:
                conditional_buf = []
                conditional_request_line = 'GET {} HTTP/1.1\r\n'.format(url.decode('utf-8')).encode('utf-8')
                conditional_header_line = 'Host: {}\r\nConnection: close\r\n' \
                        'If-modified-since: {}\r\n\r\n'.format(host, last_modified_date.decode('utf-8')).encode('utf-8')
                conditional_buf.append(conditional_request_line)
                conditional_buf.append(conditional_header_line)
                proxy_conditional_request = b''.join(conditional_buf)
                # 发送该请求
                conditional_request_socket = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
                conditional_request_socket.connect((ip, port))
                conditional_request_socket.sendall(proxy_conditional_request)
                # 接收响应
                response_buf = []
                while True:
                    data = conditional_request_socket.recv(1024)
                    if not data:
                        break
                    response_buf.append(data)
                conditional_request_socket.close()      # 关闭请求连接socket
                response_msg = b''.join(response_buf)
                response_header, response_body = response_msg.split(b'\r\n\r\n', 1)
                status_line, header_line = response_header.split(b'\r\n', 1)

                print('\r\nresponse_msg for conditional get request: ', response_msg) # ================= * 输出查看 * ==

                # 对响应做出分析
                # 初始服务器数据未修改
                if b'304' in status_line:
                    # 更新saved_date
                    new_saved_date = datetime.utcnow().strftime(GMT_FORMAT)
                    self.storage.update(url_hash, b'saved_date', new_saved_date)
                    # 直接发送给client
                    status_code = '200'
                    self.do_get_response(response_body, status_code, client_sock)       # 封装成响应报文发送给client
                else:
                    # 此处默认初始服务器该数据存在，只不过数据被修改了
                    # 获取 Date 和 Last-Modified
                    saved_date = b''
                    last_modified_date = b''
                    header_lines = header_line.split(b'\r\n')
                    for header in header_lines:
                        if header.startswith(b'Date'):
                            _, saved_date = header.split(b' ', 1)
                        if header.startswith(b'Last-Modified'):
                            _, last_modified_date = header.split(b' ', 1)
                        break
                    mapping_data = {
                        'saved_date': saved_date,
                        'last_modified_date': last_modified_date,
                        'entity_body': response_body
                    }
                    if b'200' in status_line:
                        self.storage.mset(url_hash, mapping_data)

                    _, status_code, _ = status_line.split(b' ', 2)
                    status_code = status_code.decode('utf-8')
                    self.do_get_response(response_body, status_code, client_sock)

        else:
            # 1) 获得被请求主机的地址
            # 2) 连接被请求的主机
            request_socket = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
            request_socket.connect((ip, port))
            # 3) 封装请求报文（可控性）
            request_buf = []
            # url = url.decode('utf-8')
            print('\r\nurl: ', url)                         # =========================================== * 输出查看 * ==
            request_line = 'GET {} HTTP/1.1\r\n'.format(url.decode('utf-8')).encode('utf-8')
            request_header_line = 'Host: {}\r\nConnection: close\r\n\r\n'.format(host).encode('utf-8')
            request_buf.append(request_line)
            request_buf.append(request_header_line)
            proxy_request = b''.join(request_buf)
            print('\r\nproxy_request: ', proxy_request)     # =========================================== * 输出查看 * ==
            # 4) web cache发送请求
            request_socket.sendall(proxy_request)
            # 5) 接受响应结果
            response_buf = []
            while True:
                data = request_socket.recv(1024)
                if not data:
                    break
                response_buf.append(data)
            request_socket.close()      # 关闭请求连接socket
            response_msg = b''.join(response_buf)
            # 6) 解析response_msg, 200则将结果存入数据库
            response_header, response_body = response_msg.split(b'\r\n\r\n', 1)
            status_line, header_line = response_header.split(b'\r\n', 1)
            # 获取 Date 和 Last-Modified
            saved_date = b''
            last_modified_date = b''
            header_lines = header_line.split(b'\r\n')
            for header in header_lines:
                if header.startswith(b'Date'):
                    _, saved_date = header.split(b' ', 1)
                if header.startswith(b'Last-Modified'):
                    _, last_modified_date = header.split(b' ', 1)
                    break
            mapping_data = {
                'saved_date': saved_date,
                'last_modified_date': last_modified_date,
                'entity_body': response_body
            }
            if b'200' in status_line:
                self.storage.mset(url_hash, mapping_data)

            # 7) 处理响应，发给client
            _, status_code, _ = status_line.split(b' ', 2)
            status_code = status_code.decode('utf-8')

            # print('\r\nstatus_code: ', status_code)       # =========================================== * 输出查看 * ==
            # print('\r\nresponse_body: ', response_body)
            # print('\r\nclient_sock: ', client_sock)

            self.do_get_response(response_body, status_code, client_sock)


    def do_get_response(self, *args):
        status_msg = {
            '200': 'OK',
            '301': 'Moved Permanently',
            '302': 'Temporarily Moved',
            '304': 'Not Modified',
            '400': 'Bad Request',
            '404': 'Not Found',
            '505': 'HTTP Version Not Support'
        }

        response_body, status_code, client_sock = args

        print('\r\nstatus_code: ', status_code)             # =========================================== * 输出查看 * ==
        print('\r\nresponse_body: ', response_body)
        print('\r\nclient_sock: ', client_sock)
                                
        # 封装响应报文
        response_buf = []
        status_line = 'HTTP/1.1 {} {}\r\n'.format(status_code, status_msg[status_code]).encode('utf-8')
        date = datetime.utcnow().strftime(GMT_FORMAT)
        # print('\r\ndate',date)                              # =========================================== * 输出查看 * ==
        header_line = 'Data: {}\r\nServer: WebCache\r\n\r\n'.format(date).encode('utf-8')   # 后续可根据功能再添加些首部行
        response_buf.append(status_line)
        response_buf.append(header_line)
        response_buf.append(response_body)
        proxy_response = b''.join(response_buf)
        # 发送响应报文
        # TODO: 后期会添加异常处理
        print('\r\nproxy_response: ', proxy_response)         # ========================================= * 输出查看 * ==
        client_sock.sendall(proxy_response)
        client_sock.close()

    def do_post(self):
        pass

    def do_head(self):
        pass

    def do_delete(self):
        pass

    def do_put(self):
        pass
    # ========== end do requests ===============================================================

    # 获取url的hash值
    def get_hash(self, url):
        sha1 = hashlib.sha1()
        sha1.update(url)
        return sha1.hexdigest()     # 该算法的结果有160bit，则共有2^160，很难重复

    def get_host_addr(self, header_line):
        host = ''
        header = header_line.split(b'\r\n')
        for h in header:
            h_tmp = h.decode('utf-8')
            if h_tmp.startswith('Host'):
                _, host = h_tmp.split()     # 该host type为str
                break
        try:
            ip, port = host.split(':')
        except:
            ip, port = host, 80

        return host, ip, int(port)