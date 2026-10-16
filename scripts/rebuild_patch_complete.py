import os
import subprocess

REPO = '/root/android-airplay-server'
UX = f'{REPO}/app/src/main/cpp/third_party/UxPlay'
PATCHES = f'{REPO}/app/src/main/cpp/patches/UxPlay'

print('[*] Resetting UxPlay to pristine...')
subprocess.run(['git', '-C', UX, 'reset', '--hard', 'HEAD'], check=True)
subprocess.run(['git', '-C', UX, 'clean', '-fd'], check=True)

print('[*] Applying and staging patches 0001 to 0006...')
for i in range(1, 7):
    patch_file = None
    for f in os.listdir(PATCHES):
        if f.startswith(f'000{i}-') and f.endswith('.patch'):
            patch_file = os.path.join(PATCHES, f)
            break
    print(f'Applying {patch_file}...')
    subprocess.run(['git', '-C', UX, 'apply', '--unidiff-zero', patch_file], check=True)

# Stage 0001-0006 into the git index so git diff won't include them in 0007!
subprocess.run(['git', '-C', UX, 'add', '-A'], check=True)

print('[*] Applying base 0007 patch...')
subprocess.run(['git', '-C', UX, 'apply', '--unidiff-zero', '/tmp/1391_0007.patch'], check=True)

print('[*] Applying ANNOUNCE, Transport SETUP, AirPlay 2 Buffered Audio (type 103), and PTP enhancements...')

# 0. http_request.c: add FLUSHBUFFERED and SETPEERSX custom RTSP methods
http_req_path = f'{UX}/lib/http_request.c'
with open(http_req_path, 'r') as f:
    hr = f.read()

hr_target = '''        } else if (!strncmp(data, "SETRATE ", 8)) {
            request->custom_method = strdup("SETRATE");
            allocated = malloc(datalen);
            if (allocated) {
                memcpy(allocated, data, datalen);
                memcpy(allocated, "POST    ", 8);
                parse_data = allocated;
            }
        }'''
hr_repl = hr_target + ''' else if (!strncmp(data, "FLUSHBUFFERED ", 14)) {
            request->custom_method = strdup("FLUSHBUFFERED");
            allocated = malloc(datalen);
            if (allocated) {
                memcpy(allocated, data, datalen);
                memcpy(allocated, "POST          ", 14);
                parse_data = allocated;
            }
        } else if (!strncmp(data, "SETPEERSX ", 10)) {
            request->custom_method = strdup("SETPEERSX");
            allocated = malloc(datalen);
            if (allocated) {
                memcpy(allocated, data, datalen);
                memcpy(allocated, "POST      ", 10);
                parse_data = allocated;
            }
        }'''
assert hr_target in hr, "hr_target not found in http_request.c"
hr = hr.replace(hr_target, hr_repl, 1)
with open(http_req_path, 'w') as f:
    f.write(hr)

# 1. raop.c
raop_c_path = f'{UX}/lib/raop.c'
with open(raop_c_path, 'r') as f:
    c = f.read()

target1 = '''    bool authenticated;
    bool have_active_remote;
};'''
replacement1 = '''    bool authenticated;
    bool have_active_remote;

    unsigned char aeskey[16];
    unsigned char aesiv[16];
    bool have_keys;
    int event_sock;
};'''
assert target1 in c, "target1 not found in raop.c"
c = c.replace(target1, replacement1, 1)

target_conn_init = '''    conn->raop = raop;
    conn->raop_rtp = NULL;'''
repl_conn_init = '''    conn->raop = raop;
    conn->event_sock = -1;
    conn->raop_rtp = NULL;'''
assert target_conn_init in c, "target_conn_init not found in raop.c"
c = c.replace(target_conn_init, repl_conn_init, 1)

target_conn_destroy = '''    if (conn->raop_ntp) {
        raop_ntp_destroy(conn->raop_ntp);
    }'''
repl_conn_destroy = '''    if (conn->raop_ntp) {
        raop_ntp_destroy(conn->raop_ntp);
    }
    if (conn->event_sock != -1) {
        CLOSESOCKET(conn->event_sock);
        conn->event_sock = -1;
    }'''
assert target_conn_destroy in c, "target_conn_destroy not found in raop.c"
c = c.replace(target_conn_destroy, repl_conn_destroy, 1)

target_post_cmd = '''            if (!strcmp(url, "/feedback")) {
                handler = &raop_handler_feedback;'''
repl_post_cmd = '''            if (!strcmp(url, "/feedback") || !strcmp(url, "/command")) {
                handler = &raop_handler_feedback;'''
assert target_post_cmd in c, "target_post_cmd not found in raop.c"
c = c.replace(target_post_cmd, repl_post_cmd, 1)

target2 = '''        } else if (!strcmp(method, "OPTIONS")) {
            handler = &raop_handler_options;'''
replacement2 = '''        } else if (!strcmp(method, "OPTIONS")) {
            handler = &raop_handler_options;
        } else if (!strcmp(method, "ANNOUNCE")) {
            handler = &raop_handler_announce;'''
assert target2 in c, "target2 not found in raop.c"
c = c.replace(target2, replacement2, 1)

target_methods = '''        } else if (!strcmp(method, "SETPEERS")) {
            handler = &raop_handler_setpeers;
        } else if (!strcmp(method, "SETRATEANCHORTIME") || !strcmp(method, "SETRATE")) {
            handler = &raop_handler_setrateanchortime;
        } else if (!strcmp(method, "FLUSH")) {
            handler = &raop_handler_flush;'''
repl_methods = '''        } else if (!strcmp(method, "SETPEERS") || !strcmp(method, "SETPEERSX")) {
            handler = &raop_handler_setpeers;
        } else if (!strcmp(method, "SETRATEANCHORTIME") || !strcmp(method, "SETRATE")) {
            handler = &raop_handler_setrateanchortime;
        } else if (!strcmp(method, "FLUSH")) {
            handler = &raop_handler_flush;
        } else if (!strcmp(method, "FLUSHBUFFERED")) {
            handler = &raop_handler_flushbuffered;'''
assert target_methods in c, "target_methods not found in raop.c"
c = c.replace(target_methods, repl_methods, 1)

with open(raop_c_path, 'w') as f:
    f.write(c)

# 2. raop_handlers.h
handlers_path = f'{UX}/lib/raop_handlers.h'
with open(handlers_path, 'r') as f:
    c = f.read()

hdr = '#include <openssl/evp.h>\n'
if hdr not in c:
    c = hdr + c

announce_code = '''
static int
raop_b64_decode(const char *in, unsigned char *out, int max_out) {
    if (!in) return 0;
    while (*in == ' ' || *in == '\\t') in++;
    int in_len = 0;
    while (in[in_len] && in[in_len] != '\\r' && in[in_len] != '\\n' && in[in_len] != ' ') {
        in_len++;
    }
    if (in_len == 0) return 0;
    int ret = EVP_DecodeBlock(out, (const unsigned char *)in, in_len);
    if (ret > 0 && in_len > 0 && in[in_len - 1] == '=') {
        ret--;
        if (in_len > 1 && in[in_len - 2] == '=') ret--;
    }
    return ret;
}

static void
raop_handler_announce(raop_conn_t *conn,
                      http_request_t *request, http_response_t *response,
                      char **response_data, int *response_datalen)
{
    raop_t *raop = conn->raop;
    int data_len = 0;
    const char *data = http_request_get_data(request, &data_len);
    logger_log(raop->logger, LOGGER_INFO, "raop_handler_announce received (len=%d)", data_len);

    if (data && data_len > 0) {
        const char *fpaeskey_str = strstr(data, "a=fpaeskey:");
        const char *aesiv_str = strstr(data, "a=aesiv:");

        if (fpaeskey_str && aesiv_str) {
            unsigned char eaeskey[128] = {0};
            int eaeskey_len = raop_b64_decode(fpaeskey_str + 11, eaeskey, sizeof(eaeskey));
            int aesiv_len = raop_b64_decode(aesiv_str + 8, conn->aesiv, sizeof(conn->aesiv));

            logger_log(raop->logger, LOGGER_INFO, "ANNOUNCE eaeskey_len=%d, aesiv_len=%d", eaeskey_len, aesiv_len);
            if (eaeskey_len >= 72) {
                int ret = fairplay_decrypt(conn->fairplay, eaeskey, conn->aeskey);
                logger_log(raop->logger, LOGGER_INFO, "fairplay_decrypt ANNOUNCE ret=%d", ret);

                unsigned char ecdh_secret[X25519_KEY_SIZE];
                if (pairing_get_ecdh_secret_key(conn->session, ecdh_secret)) {
                    unsigned char eaeskey_h[16];
                    memcpy(eaeskey_h, conn->aeskey, 16);
                    sha_ctx_t *ctx = sha_init();
                    sha_update(ctx, eaeskey_h, 16);
                    sha_update(ctx, ecdh_secret, 32);
                    sha_final(ctx, eaeskey_h, NULL);
                    sha_destroy(ctx);
                    memcpy(conn->aeskey, eaeskey_h, 16);
                    logger_log(raop->logger, LOGGER_INFO, "ANNOUNCE aeskey hashed with ecdh_secret");
                }
                conn->have_keys = true;
            }
        }
    }

    http_response_add_header(response, "Audio-Jack-Status", "connected; type=digital");
}
'''

target_opt = 'static void\nraop_handler_options('
assert target_opt in c, "target_opt not found in raop_handlers.h"
c = c.replace(target_opt, announce_code + '\n' + target_opt, 1)

pub_target = '"SETUP, RECORD, FLUSH, TEARDOWN, OPTIONS, GET_PARAMETER, SET_PARAMETER, SETPEERS, SETRATEANCHORTIME"'
pub_repl = '"SETUP, RECORD, FLUSH, FLUSHBUFFERED, TEARDOWN, OPTIONS, GET_PARAMETER, SET_PARAMETER, SETPEERS, SETPEERSX, SETRATEANCHORTIME, ANNOUNCE"'
assert pub_target in c, "pub_target not found in raop_handlers.h"
c = c.replace(pub_target, pub_repl, 1)

setup_target = '''    dacp_id = http_request_get_header(request, "DACP-ID");
    active_remote_header = http_request_get_header(request, "Active-Remote");'''

setup_transport_code = '''    const char *transport = http_request_get_header(request, "Transport");
    if (transport && conn->have_keys) {
        logger_log(raop->logger, LOGGER_INFO, "raop_handler_setup RAOP Transport: %s", transport);

        unsigned short remote_cport = 0;
        unsigned short timing_rport = 0;
        const char *cp = strstr(transport, "control_port=");
        if (cp) remote_cport = (unsigned short) atoi(cp + 13);
        const char *tp = strstr(transport, "timing_port=");
        if (tp) timing_rport = (unsigned short) atoi(tp + 12);

        char remote[40] = { 0 };
        utils_ipaddress_to_string(conn->remotelen, conn->remote, conn->zone_id, remote, (int) sizeof(remote));

        timing_protocol_t time_protocol = NTP;
        unsigned short timing_lport = raop->timing_lport;
        if (!conn->raop_ntp) {
            conn->raop_ntp = raop_ntp_init(raop->logger, &raop->callbacks, remote,
                                           conn->remotelen, timing_rport, &time_protocol);
            if (timing_rport) {
                raop_ntp_start(conn->raop_ntp, &timing_lport);
            }
        }

        if (!conn->raop_rtp) {
            conn->raop_rtp = raop_rtp_init(raop->logger, &raop->callbacks, conn->raop_ntp,
                                           remote, conn->remotelen, conn->aeskey, conn->aesiv);
        }

        unsigned short cport = raop->control_lport, dport = raop->data_lport;
        unsigned char ct = 2; // ALAC
        unsigned int sr = AUDIO_SAMPLE_RATE;

        if (raop->callbacks.audio_get_format) {
            unsigned short spf = 352;
            bool isMedia = true;
            bool usingScreen = false;
            uint64_t audioFormat = 0;
            raop->callbacks.audio_get_format(raop->callbacks.cls, &ct, &spf, &usingScreen, &isMedia, &audioFormat);
        }

        if (conn->raop_rtp) {
            raop_rtp_start_audio(conn->raop_rtp, &remote_cport, &cport, &dport, &ct, &sr);
            logger_log(raop->logger, LOGGER_INFO, "raop_rtp started for RAOP audio, dport=%u, cport=%u", dport, cport);
        }

        char resp_transport[256];
        snprintf(resp_transport, sizeof(resp_transport),
                 "RTP/AVP/UDP;unicast;mode=record;server_port=%u;control_port=%u;timing_port=%u",
                 dport, cport, timing_lport);
        http_response_add_header(response, "Transport", resp_transport);
        http_response_add_header(response, "Session", "1");
        http_response_add_header(response, "Audio-Jack-Status", "connected; type=digital");
        return;
    }

    dacp_id = http_request_get_header(request, "DACP-ID");
    active_remote_header = http_request_get_header(request, "Active-Remote");'''

assert setup_target in c, "setup_target not found in raop_handlers.h"
c = c.replace(setup_target, setup_transport_code, 1)

# Fix SETUP 1 eventPort and timingPeerInfo (use local IP instead of remote IP, and open listening TCP event socket)
setup1_target = '''        /* the event port is not used in mirror mode or audio mode */
        unsigned short event_port = 0;
        plist_t res_event_port_node = plist_new_uint(event_port);
        plist_dict_set_item(res_root_node, "eventPort", res_event_port_node);

        if (is_ptp) {
            plist_t res_timing_port_node = plist_new_uint(0);
            plist_dict_set_item(res_root_node, "timingPort", res_timing_port_node);

            plist_t timing_peer_info = plist_new_dict();
            plist_t addresses_array = plist_new_array();
            plist_array_append_item(addresses_array, plist_new_string(remote));
            plist_dict_set_item(timing_peer_info, "Addresses", addresses_array);
            plist_dict_set_item(timing_peer_info, "ID", plist_new_string(remote));
            plist_dict_set_item(res_root_node, "timingPeerInfo", timing_peer_info);
        } else {'''

setup1_repl = '''        /* Open a bound TCP listening socket for AirPlay 2 eventPort */
        unsigned short event_port = 0;
        int use_ipv6_event = (conn->remotelen == 16) ? 1 : 0;
        if (conn->event_sock != -1) {
            CLOSESOCKET(conn->event_sock);
            conn->event_sock = -1;
        }
        conn->event_sock = netutils_init_socket(&event_port, use_ipv6_event, 0);
        if (conn->event_sock != -1) {
            listen(conn->event_sock, 1);
        } else {
            event_port = 0;
        }
        plist_t res_event_port_node = plist_new_uint(event_port);
        plist_dict_set_item(res_root_node, "eventPort", res_event_port_node);

        if (is_ptp) {
            plist_t res_timing_port_node = plist_new_uint(0);
            plist_dict_set_item(res_root_node, "timingPort", res_timing_port_node);

            char local_ip[40] = { 0 };
            utils_ipaddress_to_string(conn->locallen, conn->local, conn->zone_id, local_ip, (int) sizeof(local_ip));
            const char *peer_ip = local_ip[0] ? local_ip : remote;

            plist_t timing_peer_info = plist_new_dict();
            plist_t addresses_array = plist_new_array();
            plist_array_append_item(addresses_array, plist_new_string(peer_ip));
            plist_dict_set_item(timing_peer_info, "Addresses", addresses_array);
            plist_dict_set_item(timing_peer_info, "ID", plist_new_string(peer_ip));
            plist_dict_set_item(res_root_node, "timingPeerInfo", timing_peer_info);
        } else {'''
assert setup1_target in c, "setup1_target not found in raop_handlers.h"
c = c.replace(setup1_target, setup1_repl, 1)

# Fix SETUP 2 for type 96, 100, and 103 (Buffered Audio over TCP with shk + audioBufferSize + streamID)
setup2_target = '''                if (conn->raop_rtp) {
                    raop_rtp_start_audio(conn->raop_rtp, &remote_cport, &cport, &dport, &ct, &sr);
                    logger_log(raop->logger, LOGGER_DEBUG, "RAOP initialized success");
                } else {
                    logger_log(raop->logger, LOGGER_ERR, "RAOP not initialized at SETUP, playing will fail!");
                    http_response_set_disconnect(response, 1);
                }

                plist_t res_stream_node = plist_new_dict();
                plist_t res_stream_data_port_node = plist_new_uint(dport);
                plist_t res_stream_control_port_node = plist_new_uint(cport);
                plist_t res_stream_type_node = plist_new_uint(type);
                plist_dict_set_item(res_stream_node, "dataPort", res_stream_data_port_node);
                plist_dict_set_item(res_stream_node, "controlPort", res_stream_control_port_node);
                plist_dict_set_item(res_stream_node, "type", res_stream_type_node);
                plist_array_append_item(res_streams_node, res_stream_node);'''

setup2_repl = '''                unsigned short spf_val = 0;
                plist_t req_stream_spf_node_top = plist_dict_get_item(req_stream_node, "spf");
                if (req_stream_spf_node_top) {
                    plist_get_uint_val(req_stream_spf_node_top, &uint_val);
                    spf_val = (unsigned short) uint_val;
                }

                unsigned char shk[32] = {0};
                uint64_t shk_len = 0;
                plist_t req_stream_shk_node = plist_dict_get_item(req_stream_node, "shk");
                if (req_stream_shk_node) {
                    char *shk_data = NULL;
                    plist_get_data_val(req_stream_shk_node, &shk_data, &shk_len);
                    if (shk_data && shk_len <= sizeof(shk)) {
                        memcpy(shk, shk_data, shk_len);
                    }
                    if (shk_data) free(shk_data);
                }

                if (conn->raop_rtp) {
                    if (type == 103) {
                        raop_rtp_start_buffered_audio(conn->raop_rtp, &remote_cport, &cport, &dport,
                                                      &ct, &sr, &spf_val, shk, (int)shk_len);
                        logger_log(raop->logger, LOGGER_INFO, "RAOP buffered audio (type 103) initialized: TCP dport=%u, UDP cport=%u, ct=%u, spf=%u, shk_len=%llu",
                                   dport, cport, ct, spf_val, (unsigned long long)shk_len);
                    } else {
                        raop_rtp_start_audio(conn->raop_rtp, &remote_cport, &cport, &dport, &ct, &sr);
                        logger_log(raop->logger, LOGGER_DEBUG, "RAOP initialized success");
                    }
                } else {
                    logger_log(raop->logger, LOGGER_ERR, "RAOP not initialized at SETUP, playing will fail!");
                    http_response_set_disconnect(response, 1);
                }

                plist_t res_stream_node = plist_new_dict();
                plist_t res_stream_data_port_node = plist_new_uint(dport);
                plist_t res_stream_control_port_node = plist_new_uint(cport);
                plist_t res_stream_type_node = plist_new_uint(type);
                plist_dict_set_item(res_stream_node, "dataPort", res_stream_data_port_node);
                plist_dict_set_item(res_stream_node, "controlPort", res_stream_control_port_node);
                plist_dict_set_item(res_stream_node, "type", res_stream_type_node);
                if (type == 103) {
                    plist_dict_set_item(res_stream_node, "audioBufferSize", plist_new_uint(8388608));
                }

                plist_t req_stream_id_node = plist_dict_get_item(req_stream_node, "streamID");
                if (req_stream_id_node) {
                    uint64_t stream_id = 1;
                    plist_get_uint_val(req_stream_id_node, &stream_id);
                    plist_dict_set_item(res_stream_node, "streamID", plist_new_uint(stream_id));
                } else {
                    plist_dict_set_item(res_stream_node, "streamID", plist_new_uint(type == 103 ? 1 : 0));
                }

                plist_t req_stream_conn_id = plist_dict_get_item(req_stream_node, "streamConnectionID");
                if (req_stream_conn_id) {
                    uint64_t conn_id = 0;
                    plist_get_uint_val(req_stream_conn_id, &conn_id);
                    plist_dict_set_item(res_stream_node, "streamConnectionID", plist_new_uint(conn_id));
                }
                plist_array_append_item(res_streams_node, res_stream_node);'''
assert setup2_target in c, "setup2_target not found in raop_handlers.h"
c = c.replace(setup2_target, setup2_repl, 1)

# Implement SETRATEANCHORTIME and FLUSHBUFFERED handlers in raop_handlers.h
setrate_target = '''static void
raop_handler_setrateanchortime(raop_conn_t *conn,
                               http_request_t *request, http_response_t *response,
                               char **response_data, int *response_datalen)
{
    raop_t *raop = conn->raop;
    logger_log(raop->logger, LOGGER_INFO, "raop_handler_setrateanchortime: playback anchor acknowledged");
}'''

setrate_repl = '''static void
raop_handler_setrateanchortime(raop_conn_t *conn,
                               http_request_t *request, http_response_t *response,
                               char **response_data, int *response_datalen)
{
    raop_t *raop = conn->raop;
    const char *data = NULL;
    int data_len = 0;
    data = http_request_get_data(request, &data_len);
    uint64_t rate_val = 0;
    uint64_t rtp_time_val = 0;
    if (data && data_len > 0) {
        plist_t req_root_node = NULL;
        plist_from_bin(data, data_len, &req_root_node);
        if (req_root_node) {
            plist_t rate_node = plist_dict_get_item(req_root_node, "rate");
            if (rate_node) {
                plist_type t = plist_get_node_type(rate_node);
                if (t == PLIST_REAL) {
                    double d = 0;
                    plist_get_real_val(rate_node, &d);
                    rate_val = (d > 0.5) ? 1 : 0;
                } else if (t == PLIST_UINT) {
                    plist_get_uint_val(rate_node, &rate_val);
                }
            }
            plist_t rtp_node = plist_dict_get_item(req_root_node, "rtpTime");
            if (rtp_node) plist_get_uint_val(rtp_node, &rtp_time_val);
            plist_free(req_root_node);
        }
    }
    uint64_t net_secs = 0;
    uint64_t net_frac = 0;
    if (data && data_len > 0) {
        plist_t req_root_node2 = NULL;
        plist_from_bin(data, data_len, &req_root_node2);
        if (req_root_node2) {
            plist_t s_node = plist_dict_get_item(req_root_node2, "networkTimeSecs");
            if (s_node) plist_get_uint_val(s_node, &net_secs);
            plist_t f_node = plist_dict_get_item(req_root_node2, "networkTimeFrac");
            if (f_node) plist_get_uint_val(f_node, &net_frac);
            plist_free(req_root_node2);
        }
    }
    logger_log(raop->logger, LOGGER_INFO, "raop_handler_setrateanchortime: rate=%llu rtpTime=%llu netSecs=%llu netFrac=%llu",
               (unsigned long long)rate_val, (unsigned long long)rtp_time_val,
               (unsigned long long)net_secs, (unsigned long long)net_frac);
    if (conn->raop_rtp) {
        raop_rtp_set_anchor(conn->raop_rtp, (uint32_t)rate_val, (uint32_t)rtp_time_val, net_secs, net_frac);
    }
}

static void
raop_handler_flushbuffered(raop_conn_t *conn,
                           http_request_t *request, http_response_t *response,
                           char **response_data, int *response_datalen)
{
    raop_t *raop = conn->raop;
    const char *data = NULL;
    int data_len = 0;
    data = http_request_get_data(request, &data_len);
    int64_t flush_from_seq = -1;
    int64_t flush_until_seq = -1;
    uint64_t flush_until_ts = 0;
    if (data && data_len > 0) {
        plist_t req_root_node = NULL;
        plist_from_bin(data, data_len, &req_root_node);
        if (req_root_node) {
            plist_t from_node = plist_dict_get_item(req_root_node, "flushFromSeq");
            if (from_node) {
                uint64_t v = 0;
                plist_get_uint_val(from_node, &v);
                flush_from_seq = (int64_t)v;
            }
            plist_t until_node = plist_dict_get_item(req_root_node, "flushUntilSeq");
            if (until_node) {
                uint64_t v = 0;
                plist_get_uint_val(until_node, &v);
                flush_until_seq = (int64_t)v;
            }
            plist_t ts_node = plist_dict_get_item(req_root_node, "flushUntilTS");
            if (ts_node) {
                plist_get_uint_val(ts_node, &flush_until_ts);
            }
            plist_free(req_root_node);
        }
    }
    logger_log(raop->logger, LOGGER_INFO, "raop_handler_flushbuffered: fromSeq=%lld untilSeq=%lld untilTS=%llu",
               (long long)flush_from_seq, (long long)flush_until_seq, (unsigned long long)flush_until_ts);
    if (conn->raop_rtp) {
        raop_rtp_flush_buffered(conn->raop_rtp, flush_from_seq, flush_until_seq, (uint32_t)flush_until_ts);
    }
}'''
assert setrate_target in c, "setrate_target not found in raop_handlers.h"
c = c.replace(setrate_target, setrate_repl, 1)

# Teardown connection close check
teardown_old = 'http_response_add_header(response, "Connection", "close");'
teardown_new = 'if (!teardown_96 && !teardown_110) http_response_add_header(response, "Connection", "close");'
assert teardown_old in c, "teardown_old not found in raop_handlers.h"
c = c.replace(teardown_old, teardown_new, 1)

with open(handlers_path, 'w') as f:
    f.write(c)

# 3. raop_rtp.h and raop_rtp.c
rtp_h_path = f'{UX}/lib/raop_rtp.h'
with open(rtp_h_path, 'r') as f:
    rtp_h = f.read()

rtp_h_target = 'void raop_rtp_set_volume(raop_rtp_t *raop_rtp, float volume);'
rtp_h_repl = '''void raop_rtp_start_buffered_audio(raop_rtp_t *raop_rtp, unsigned short *control_rport,
                                   unsigned short *control_lport, unsigned short *data_lport,
                                   unsigned char *ct, unsigned int *sr, unsigned short *spf,
                                   const unsigned char *shk, int shk_len);
void raop_rtp_set_anchor(raop_rtp_t *raop_rtp, uint32_t rate, uint32_t rtp_time, uint64_t net_secs, uint64_t net_frac);
void raop_rtp_flush_buffered(raop_rtp_t *raop_rtp, int64_t flush_from_seq, int64_t flush_until_seq, uint32_t flush_until_ts);
void raop_rtp_set_initial_sync(raop_rtp_t *raop_rtp);
void raop_rtp_set_volume(raop_rtp_t *raop_rtp, float volume);'''
assert rtp_h_target in rtp_h, "rtp_h_target not found in raop_rtp.h"
rtp_h = rtp_h.replace(rtp_h_target, rtp_h_repl, 1)
with open(rtp_h_path, 'w') as f:
    f.write(rtp_h)

rtp_c_path = f'{UX}/lib/raop_rtp.c'
with open(rtp_c_path, 'r') as f:
    rtp_c = f.read()

# Include openssl/evp.h and unistd.h in raop_rtp.c
rtp_c_hdr_target = '#include <stdbool.h>'
rtp_c_hdr_repl = '#include <stdbool.h>\n#include <unistd.h>\n#include <openssl/evp.h>'
assert rtp_c_hdr_target in rtp_c, "rtp_c_hdr_target not found in raop_rtp.c"
rtp_c = rtp_c.replace(rtp_c_hdr_target, rtp_c_hdr_repl, 1)

# Add buffered audio fields to struct raop_rtp_s
rtp_struct_target = '''    /* audio compression type: ct = 2 (ALAC), ct = 8 (AAC_ELD) (ct = 4 would be AAC-MAIN) */
    unsigned char ct;
};'''
rtp_struct_repl = '''    /* audio compression type: ct = 2 (ALAC), ct = 8 (AAC_ELD) (ct = 4 would be AAC-MAIN) */
    unsigned char ct;

    /* AirPlay 2 Buffered Audio (type 103 over TCP with ChaCha20-Poly1305) */
    int tsock;
    int tcp_client_sock;
    bool is_buffered;
    unsigned short spf;
    unsigned char shk[32];
    int shk_len;
    uint32_t buffered_rate;
    uint32_t anchor_rtp;
    bool anchor_valid;
    bool has_anchored;
    uint64_t anchor_local_ns;
    int64_t flush_from_seq;
    int64_t flush_until_seq;
    uint32_t flush_until_ts;
    bool buffered_flush_pending;
};'''
assert rtp_struct_target in rtp_c, "rtp_struct_target not found in raop_rtp.c"
rtp_c = rtp_c.replace(rtp_struct_target, rtp_struct_repl, 1)

# Initialize sockets in raop_rtp_init
rtp_init_target = '''    raop_rtp->running = 0;
    raop_rtp->joined = 1;
    raop_rtp->flush = NO_FLUSH;'''
rtp_init_repl = '''    raop_rtp->running = 0;
    raop_rtp->joined = 1;
    raop_rtp->flush = NO_FLUSH;
    raop_rtp->csock = -1;
    raop_rtp->dsock = -1;
    raop_rtp->tsock = -1;
    raop_rtp->tcp_client_sock = -1;
    raop_rtp->flush_from_seq = -1;
    raop_rtp->flush_until_seq = -1;
    raop_rtp->anchor_valid = false;
    raop_rtp->has_anchored = false;
    raop_rtp->anchor_local_ns = 0;
    raop_rtp->anchor_rtp = 0;'''
assert rtp_init_target in rtp_c, "rtp_init_target not found in raop_rtp.c"
rtp_c = rtp_c.replace(rtp_init_target, rtp_init_repl, 1)

# Flush raop_buffer in raop_rtp_process_events when flush != NO_FLUSH
flush_ev_target = '''    /* Handle flush if requested */
    if (flush != NO_FLUSH) {
        if (raop_rtp->callbacks.audio_flush) {
            raop_rtp->callbacks.audio_flush(raop_rtp->callbacks.cls);
        }
    }'''
flush_ev_repl = '''    /* Handle flush if requested */
    if (flush != NO_FLUSH) {
        raop_buffer_flush(raop_rtp->buffer, flush);
        if (raop_rtp->callbacks.audio_flush) {
            raop_rtp->callbacks.audio_flush(raop_rtp->callbacks.cls);
        }
    }'''
assert flush_ev_target in rtp_c, "flush_ev_target not found in raop_rtp.c"
rtp_c = rtp_c.replace(flush_ev_target, flush_ev_repl, 1)

# Enqueue & initial_sync handling in raop_rtp_thread_udp
enqueue_target = '''            raop_buffer_enqueue(raop_rtp->buffer, packet, packetlen, 1);

            if (!raop_rtp->initial_sync) {
                /* wait until the first sync before dequeing ALAC */
                continue;
            } else {'''
enqueue_repl = '''            raop_buffer_enqueue(raop_rtp->buffer, packet, packetlen, 1);

            if (!raop_rtp->initial_sync) {
                if (raop_rtp->ntp == NULL || raop_ntp_get_port(raop_rtp->ntp) == 0) {
                    raop_rtp->initial_sync = true;
                    raop_rtp->rtp_sync = byteutils_get_int_be(packet, 4);
                    logger_log(raop_rtp->logger, LOGGER_INFO, "raop_rtp initial_sync auto-enabled on packet in PTP mode (rtp_sync=%u)", raop_rtp->rtp_sync);
                } else {
                    /* wait until the first sync before dequeing ALAC */
                    continue;
                }
            } else {'''
assert enqueue_target in rtp_c, "enqueue_target not found in raop_rtp.c"
rtp_c = rtp_c.replace(enqueue_target, enqueue_repl, 1)

# 0x54 / 0x57 sync handling: set sync_offset ONLY ONCE on the first sync packet (when client_ntp_sync_prev == 0)
sync_target = """            } else if (type_c == 0x54 && packetlen >= 20) {
                /* packet[0] = 0x90 (first sync ?) or 0x80 (subsequent ones)"""
sync_repl = """            } else if ((type_c == 0x54 || type_c == 0x57) && packetlen >= 20) {
                /* packet[0] = 0x90 (first sync ?) or 0x80 (subsequent ones)"""
assert sync_target in rtp_c, "sync_target not found in raop_rtp.c"
rtp_c = rtp_c.replace(sync_target, sync_repl, 1)

raw_target = """                raop_rtp->rtp_sync = byteutils_get_int_be(packet, 4);
                uint64_t sync_ntp_raw = byteutils_get_long_be(packet, 8);
                raop_rtp->client_ntp_sync = raop_remote_timestamp_to_nano_seconds(raop_rtp->ntp, sync_ntp_raw);"""
raw_repl = """                raop_rtp->rtp_sync = byteutils_get_int_be(packet, 4);
                if (type_c == 0x57) {
                    raop_rtp->client_ntp_sync = byteutils_get_long_be(packet, 8);
                } else {
                    uint64_t sync_ntp_raw = byteutils_get_long_be(packet, 8);
                    raop_rtp->client_ntp_sync = raop_remote_timestamp_to_nano_seconds(raop_rtp->ntp, sync_ntp_raw);
                }
                if (client_ntp_sync_prev == 0 && raop_rtp->ntp) {
                    uint64_t local_now = raop_ntp_get_local_time();
                    raop_ntp_set_sync_offset(raop_rtp->ntp, (int64_t)raop_rtp->client_ntp_sync - (int64_t)local_now);
                }
                logger_log(raop_rtp->logger, LOGGER_INFO, "raop_rtp sync update (type_c=0x%02x): rtp_sync=%u client_sync=%llu", type_c, raop_rtp->rtp_sync, (unsigned long long)raop_rtp->client_ntp_sync);"""
assert raw_target in rtp_c, "raw_target not found in raop_rtp.c"
rtp_c = rtp_c.replace(raw_target, raw_repl, 1)

# Add TCP Buffered Audio thread and helper functions before raop_rtp_set_volume
buffered_code = '''
static inline uint64_t
raop_mono_ns(void)
{
    struct timespec ts;
    clock_gettime(CLOCK_MONOTONIC, &ts);
    return (uint64_t)ts.tv_sec * SECOND_IN_NSECS + (uint64_t)ts.tv_nsec;
}

static int
raop_rtp_read_exact(raop_rtp_t *raop_rtp, int sock, unsigned char *buf, int len)
{
    int got = 0;
    while (got < len) {
        if (raop_rtp_process_events(raop_rtp, NULL)) {
            return -1;
        }
        fd_set rfds;
        struct timeval tv;
        tv.tv_sec = 0;
        tv.tv_usec = 10000; /* 10ms */
        FD_ZERO(&rfds);
        FD_SET(sock, &rfds);
        int nfds = sock + 1;
        if (raop_rtp->csock != -1) {
            FD_SET(raop_rtp->csock, &rfds);
            if (raop_rtp->csock >= nfds) nfds = raop_rtp->csock + 1;
        }
        int sret = select(nfds, &rfds, NULL, NULL, &tv);
        if (sret == 0) {
            continue;
        } else if (sret < 0) {
            if (errno == EINTR) continue;
            return -1;
        }
        if (raop_rtp->csock != -1 && FD_ISSET(raop_rtp->csock, &rfds)) {
            unsigned char cbuf[256];
            recvfrom(raop_rtp->csock, (char *)cbuf, sizeof(cbuf), 0, NULL, NULL);
        }
        if (FD_ISSET(sock, &rfds)) {
            int r = recv(sock, (char *)(buf + got), len - got, 0);
            if (r <= 0) {
                if (r < 0 && (errno == EINTR || errno == EAGAIN || errno == EWOULDBLOCK)) {
                    continue;
                }
                return -1;
            }
            got += r;
        }
    }
    return got;
}

static int
raop_rtp_decrypt_chacha(const unsigned char *key,
                        const unsigned char *nonce8,
                        const unsigned char *aad, int aad_len,
                        const unsigned char *tag,
                        const unsigned char *ciphertext, int ciphertext_len,
                        unsigned char *plaintext, int *plaintext_len)
{
    if (ciphertext_len < 0) return -1;
    unsigned char iv[12];
    memset(iv, 0, 4);
    memcpy(iv + 4, nonce8, 8);

    EVP_CIPHER_CTX *ctx = EVP_CIPHER_CTX_new();
    if (!ctx) return -1;

    int ok = 0;
    int len = 0;
    int out_len = 0;

    if (EVP_DecryptInit_ex(ctx, EVP_chacha20_poly1305(), NULL, NULL, NULL) != 1) goto done;
    if (EVP_CIPHER_CTX_ctrl(ctx, EVP_CTRL_AEAD_SET_IVLEN, 12, NULL) != 1) goto done;
    if (EVP_DecryptInit_ex(ctx, NULL, NULL, key, iv) != 1) goto done;
    if (aad && aad_len > 0) {
        if (EVP_DecryptUpdate(ctx, NULL, &len, aad, aad_len) != 1) goto done;
    }
    if (EVP_DecryptUpdate(ctx, plaintext, &len, ciphertext, ciphertext_len) != 1) goto done;
    out_len = len;
    if (EVP_CIPHER_CTX_ctrl(ctx, EVP_CTRL_AEAD_SET_TAG, 16, (void *)tag) != 1) goto done;
    if (EVP_DecryptFinal_ex(ctx, plaintext + len, &len) <= 0) goto done;
    out_len += len;
    *plaintext_len = out_len;
    ok = 1;

done:
    EVP_CIPHER_CTX_free(ctx);
    return ok ? 0 : -1;
}

static THREAD_RETVAL
raop_rtp_thread_tcp(void *arg)
{
    raop_rtp_t *raop_rtp = arg;
    assert(raop_rtp);
    logger_log(raop_rtp->logger, LOGGER_INFO, "raop_rtp_thread_tcp started, waiting for TCP connection on port %u", raop_rtp->data_lport);

    unsigned char *packet = (unsigned char *)malloc(65536);
    unsigned char *plaintext = (unsigned char *)malloc(65536);
    if (!packet || !plaintext) {
        free(packet);
        free(plaintext);
        goto exit_thread;
    }

    uint64_t thread_start_ns = raop_mono_ns();
    const uint64_t lead_ns = 600000000ULL; /* Keep ~600ms ahead in TimelineBuffer */
    const unsigned int spf = (raop_rtp->spf > 0) ? raop_rtp->spf : 1024;
    uint32_t pkt_count = 0;
    int client_sock = -1;

    while (1) {
        if (raop_rtp_process_events(raop_rtp, NULL)) {
            break;
        }

        if (client_sock == -1) {
            fd_set rfds;
            struct timeval tv;
            tv.tv_sec = 0;
            tv.tv_usec = 20000; /* 20ms */
            FD_ZERO(&rfds);
            FD_SET(raop_rtp->tsock, &rfds);
            int ret = select(raop_rtp->tsock + 1, &rfds, NULL, NULL, &tv);
            if (ret > 0 && FD_ISSET(raop_rtp->tsock, &rfds)) {
                struct sockaddr_storage caddr;
                socklen_t caddrlen = sizeof(caddr);
                client_sock = accept(raop_rtp->tsock, (struct sockaddr *)&caddr, &caddrlen);
                if (client_sock != -1) {
                    MUTEX_LOCK(raop_rtp->run_mutex);
                    raop_rtp->tcp_client_sock = client_sock;
                    MUTEX_UNLOCK(raop_rtp->run_mutex);
                    logger_log(raop_rtp->logger, LOGGER_INFO, "raop_rtp_thread_tcp: accepted TCP connection (sock=%d)", client_sock);
                }
            } else if (ret < 0 && errno != EINTR) {
                logger_log(raop_rtp->logger, LOGGER_ERR, "raop_rtp_thread_tcp: select on tsock failed: %d", errno);
                break;
            }
            continue;
        }

        /* Handle pending audio flush */
        MUTEX_LOCK(raop_rtp->run_mutex);
        bool flush_pend = raop_rtp->buffered_flush_pending;
        int64_t f_from = raop_rtp->flush_from_seq;
        int64_t f_until = raop_rtp->flush_until_seq;
        if (flush_pend && f_from <= 0) {
            raop_rtp->buffered_flush_pending = false;
        }
        MUTEX_UNLOCK(raop_rtp->run_mutex);

        if (flush_pend && f_from <= 0) {
            if (raop_rtp->callbacks.audio_flush) {
                raop_rtp->callbacks.audio_flush(raop_rtp->callbacks.cls);
            }
        }

        unsigned char len_buf[2];
        if (raop_rtp_read_exact(raop_rtp, client_sock, len_buf, 2) < 0) {
            logger_log(raop_rtp->logger, LOGGER_INFO, "raop_rtp_thread_tcp: client disconnected (sock=%d), waiting for new connection", client_sock);
            MUTEX_LOCK(raop_rtp->run_mutex);
            CLOSESOCKET(client_sock);
            client_sock = -1;
            raop_rtp->tcp_client_sock = -1;
            MUTEX_UNLOCK(raop_rtp->run_mutex);
            continue;
        }
        uint16_t packet_len = ((uint16_t)len_buf[0] << 8) | (uint16_t)len_buf[1];
        if (packet_len < 2) {
            continue;
        }
        int nread = (int)packet_len - 2;
        if (raop_rtp_read_exact(raop_rtp, client_sock, packet, nread) < 0) {
            logger_log(raop_rtp->logger, LOGGER_INFO, "raop_rtp_thread_tcp: client disconnected while reading body (sock=%d)", client_sock);
            MUTEX_LOCK(raop_rtp->run_mutex);
            CLOSESOCKET(client_sock);
            client_sock = -1;
            raop_rtp->tcp_client_sock = -1;
            MUTEX_UNLOCK(raop_rtp->run_mutex);
            continue;
        }
        if (nread < 36) {
            continue;
        }

        /* 23-bit sequence number */
        uint32_t seqnum = (((uint32_t)(packet[1] & 0x7f) << 16) |
                           ((uint32_t)packet[2] << 8) |
                           (uint32_t)packet[3]);
        uint32_t rtp_timestamp = byteutils_get_int_be(packet, 4);

        /* Check FLUSHBUFFERED range */
        MUTEX_LOCK(raop_rtp->run_mutex);
        f_from = raop_rtp->flush_from_seq;
        f_until = raop_rtp->flush_until_seq;
        flush_pend = raop_rtp->buffered_flush_pending;
        if (f_until >= 0) {
            if (f_from <= 0 || seqnum >= (uint32_t)f_from) {
                if (seqnum < (uint32_t)f_until) {
                    if (flush_pend) {
                        raop_rtp->buffered_flush_pending = false;
                        MUTEX_UNLOCK(raop_rtp->run_mutex);
                        if (raop_rtp->callbacks.audio_flush) {
                            raop_rtp->callbacks.audio_flush(raop_rtp->callbacks.cls);
                        }
                                            } else {
                        MUTEX_UNLOCK(raop_rtp->run_mutex);
                    }
                    continue; /* Drop flushed packet at wire speed */
                } else {
                    /* Reached flushUntilSeq: clear flush state */
                    raop_rtp->flush_from_seq = -1;
                    raop_rtp->flush_until_seq = -1;
                    raop_rtp->buffered_flush_pending = false;
                    MUTEX_UNLOCK(raop_rtp->run_mutex);
                    if (flush_pend && raop_rtp->callbacks.audio_flush) {
                        raop_rtp->callbacks.audio_flush(raop_rtp->callbacks.cls);
                    }
                                        logger_log(raop_rtp->logger, LOGGER_INFO, "raop_rtp_thread_tcp: flush completed at seqnum=%u rtp=%u", seqnum, rtp_timestamp);
                }
            } else {
                MUTEX_UNLOCK(raop_rtp->run_mutex);
            }
        } else {
            MUTEX_UNLOCK(raop_rtp->run_mutex);
        }

        const unsigned char *aad = packet + 4; /* 8 bytes: timestamp + ssrc */
        const unsigned char *ciphertext = packet + 12;
        int ciphertext_len = nread - 36;
        const unsigned char *tag = packet + nread - 24;
        const unsigned char *nonce8 = packet + nread - 8;

        int plaintext_len = 0;
        if (raop_rtp_decrypt_chacha(raop_rtp->shk, nonce8, aad, 8, tag,
                                    ciphertext, ciphertext_len,
                                    plaintext, &plaintext_len) < 0) {
            if (pkt_count < 10) {
                logger_log(raop_rtp->logger, LOGGER_ERR, "raop_rtp_thread_tcp: ChaCha20-Poly1305 decrypt failed at seqnum=%u nread=%d", seqnum, nread);
            }
            continue;
        }

        if (pkt_count == 0 || (pkt_count % 200) == 0) {
            logger_log(raop_rtp->logger, LOGGER_INFO, "raop_rtp_thread_tcp: decoded pkt #%u seqnum=%u rtp=%u len=%d",
                       pkt_count, seqnum, rtp_timestamp, plaintext_len);
        }
        pkt_count++;

        /* Anchor-time sync (PTP fallback: unrooted Android cannot bind ports 319/320).
         * Hold the packet until SETRATEANCHORTIME (rate=1) has supplied an anchor, then
         * derive its absolute presentation time:
         *   pts = anchor_local_ns + (rtp - anchor_rtp) * ns_per_frame
         * Every receiver in the group gets the same anchor at ~the same instant, so they
         * all schedule a given rtp frame at the same wall-clock moment. */
        uint64_t pts_local_ns = 0;
        for (;;) {
            if (raop_rtp_process_events(raop_rtp, NULL)) goto loop_end;
            MUTEX_LOCK(raop_rtp->run_mutex);
            bool a_valid = raop_rtp->anchor_valid;
            bool a_ever = raop_rtp->has_anchored;
            uint64_t a_local = raop_rtp->anchor_local_ns;
            uint32_t a_rtp = raop_rtp->anchor_rtp;
            bool in_flush = raop_rtp->flush_until_seq >= 0 &&
                            (raop_rtp->flush_from_seq <= 0 || seqnum >= (uint32_t)raop_rtp->flush_from_seq) &&
                            seqnum < (uint32_t)raop_rtp->flush_until_seq;
            MUTEX_UNLOCK(raop_rtp->run_mutex);
            if (in_flush) goto next_packet;
            uint64_t now_ns = raop_mono_ns();
            if (a_valid) {
                int32_t frames_from_anchor = (int32_t)(rtp_timestamp - a_rtp);
                int64_t off_ns = (int64_t)((double)frames_from_anchor * raop_rtp->rtp_clock_rate);
                /* Target PTS is the exact presentation time scheduled by the AirPlay 2 sender */
                int64_t target_pts = (int64_t)a_local + off_ns;
                pts_local_ns = (uint64_t)(target_pts > 0 ? target_pts : 0);
                /* pace: keep decoded audio at most lead_ns ahead of its play time */
                if ((int64_t)(pts_local_ns - now_ns) <= (int64_t)lead_ns) break;
            } else if (!a_ever && (now_ns - thread_start_ns) > 2000000000ULL) {
                /* sender never anchored: legacy unscheduled playback */
                break;
            }
            usleep(5000);
        }
        
        audio_decode_struct audio_data;
        memset(&audio_data, 0, sizeof(audio_data));
        audio_data.rtp_time = rtp_timestamp;
        audio_data.seqnum = (unsigned short)(seqnum & 0xffff);
        audio_data.data_len = plaintext_len;
        audio_data.data = plaintext;
        audio_data.ct = raop_rtp->ct;
        audio_data.ntp_time_remote = 0;
        audio_data.ntp_time_local = pts_local_ns;

        raop_rtp->callbacks.audio_process(raop_rtp->callbacks.cls, raop_rtp->ntp, &audio_data);

next_packet:
        ;
    }

loop_end:
    free(packet);
    free(plaintext);

exit_thread:
    MUTEX_LOCK(raop_rtp->run_mutex);
    if (client_sock != -1) {
        CLOSESOCKET(client_sock);
        client_sock = -1;
    }
    if (raop_rtp->tcp_client_sock != -1) {
        CLOSESOCKET(raop_rtp->tcp_client_sock);
        raop_rtp->tcp_client_sock = -1;
    }
    raop_rtp->running = 0;
    MUTEX_UNLOCK(raop_rtp->run_mutex);
    logger_log(raop_rtp->logger, LOGGER_INFO, "raop_rtp_thread_tcp exiting");
    return 0;
}

void
raop_rtp_start_buffered_audio(raop_rtp_t *raop_rtp, unsigned short *control_rport,
                              unsigned short *control_lport, unsigned short *data_lport,
                              unsigned char *ct, unsigned int *sr, unsigned short *spf,
                              const unsigned char *shk, int shk_len)
{
    assert(raop_rtp);
    logger_log(raop_rtp->logger, LOGGER_INFO, "raop_rtp starting buffered audio (type 103)");

    MUTEX_LOCK(raop_rtp->run_mutex);
    if (raop_rtp->running) {
        *control_lport = raop_rtp->control_lport;
        *data_lport = raop_rtp->data_lport;
        if (shk && shk_len > 0) {
            int copy_len = (shk_len > 32) ? 32 : shk_len;
            memcpy(raop_rtp->shk, shk, copy_len);
            raop_rtp->shk_len = copy_len;
        }
        if (spf && *spf > 0) raop_rtp->spf = *spf;
        if (ct && *ct > 0) raop_rtp->ct = *ct;
        logger_log(raop_rtp->logger, LOGGER_INFO,
                   "raop_rtp_start_buffered_audio already running, reusing session: dport=%u cport=%u",
                   *data_lport, *control_lport);
        MUTEX_UNLOCK(raop_rtp->run_mutex);
        return;
    }

    if (!raop_rtp->joined) {
        THREAD_JOIN(raop_rtp->thread);
        raop_rtp->joined = 1;
    }

    raop_rtp->is_buffered = true;
    raop_rtp->ct = *ct;
    raop_rtp->spf = (*spf > 0) ? *spf : 1024;
    raop_rtp->rtp_clock_rate = (double)SECOND_IN_NSECS / (double)(*sr > 0 ? *sr : 44100);
    memset(raop_rtp->shk, 0, sizeof(raop_rtp->shk));
    if (shk && shk_len > 0) {
        int copy_len = (shk_len > 32) ? 32 : shk_len;
        memcpy(raop_rtp->shk, shk, copy_len);
        raop_rtp->shk_len = copy_len;
    }
    raop_rtp->buffered_rate = 0;
    raop_rtp->anchor_valid = false;
    raop_rtp->has_anchored = false;
    raop_rtp->anchor_local_ns = 0;
    raop_rtp->anchor_rtp = 0;
    raop_rtp->flush_from_seq = -1;
    raop_rtp->flush_until_seq = -1;
    raop_rtp->flush_until_ts = 0;
    raop_rtp->buffered_flush_pending = false;

    int use_ipv6 = (raop_rtp->remote_saddr.ss_family == AF_INET6) ? 1 : 0;
    unsigned short cport = *control_lport;
    unsigned short dport = *data_lport;
    int csock = netutils_init_socket(&cport, use_ipv6, 1); /* UDP control socket */
    int tsock = netutils_init_socket(&dport, use_ipv6, 0); /* TCP data listening socket */
    if (csock == -1 || tsock == -1 || listen(tsock, 1) < 0) {
        logger_log(raop_rtp->logger, LOGGER_ERR, "raop_rtp_start_buffered_audio socket init failed (csock=%d tsock=%d)", csock, tsock);
        if (csock != -1) CLOSESOCKET(csock);
        if (tsock != -1) CLOSESOCKET(tsock);
        MUTEX_UNLOCK(raop_rtp->run_mutex);
        return;
    }

    raop_rtp->csock = csock;
    raop_rtp->dsock = -1;
    raop_rtp->tsock = tsock;
    raop_rtp->tcp_client_sock = -1;
    raop_rtp->control_lport = cport;
    raop_rtp->data_lport = dport;
    raop_rtp->control_rport = *control_rport;
    *control_lport = cport;
    *data_lport = dport;

    raop_rtp->running = 1;
    raop_rtp->joined = 0;
    THREAD_CREATE(raop_rtp->thread, raop_rtp_thread_tcp, raop_rtp);
    MUTEX_UNLOCK(raop_rtp->run_mutex);
}

void
raop_rtp_set_anchor(raop_rtp_t *raop_rtp, uint32_t rate, uint32_t rtp_time, uint64_t net_secs, uint64_t net_frac)
{
    if (!raop_rtp) return;
    bool do_flush = false;
    MUTEX_LOCK(raop_rtp->run_mutex);
    if (raop_rtp->is_buffered && raop_rtp->buffered_rate != 0 && rate == 0) {
        do_flush = true;
    }
    raop_rtp->buffered_rate = rate;
    if (rate != 0) {
        uint64_t local_mono_ns = raop_mono_ns();
        uint64_t calc_anchor = local_mono_ns;
        if (net_secs > 0) {
            uint64_t secs = net_secs;
            if (secs > 2000000000ULL) {
                secs -= 2208988800ULL; /* Convert NTP epoch (1900) to POSIX epoch (1970) */
            }
            uint64_t frac_ns = 0;
            if (net_frac > 0xFFFFFFFFULL) {
                frac_ns = (uint64_t)(((double)net_frac / 18446744073709551616.0) * 1e9);
            } else {
                frac_ns = (uint64_t)(((double)net_frac / 4294967296.0) * 1e9);
            }
            int64_t target_real_ns = (int64_t)secs * 1000000000LL + (int64_t)frac_ns;
            struct timespec ts;
            clock_gettime(CLOCK_REALTIME, &ts);
            int64_t now_real_ns = (int64_t)ts.tv_sec * 1000000000LL + ts.tv_nsec;
            int64_t diff_ns = target_real_ns - now_real_ns;
            if (diff_ns > -5000000000LL && diff_ns < 10000000000LL) {
                calc_anchor = (uint64_t)((int64_t)local_mono_ns + diff_ns);
                logger_log(raop_rtp->logger, LOGGER_INFO,
                           "raop_rtp_set_anchor: synced via networkTime, diff=%lldms",
                           (long long)(diff_ns / 1000000LL));
            } else {
                logger_log(raop_rtp->logger, LOGGER_INFO,
                           "raop_rtp_set_anchor: networkTime diff out of range (%lldms), using local arrival",
                           (long long)(diff_ns / 1000000LL));
            }
        } else {
            logger_log(raop_rtp->logger, LOGGER_INFO,
                       "raop_rtp_set_anchor: net_secs=0, using local arrival");
        }
        raop_rtp->anchor_local_ns = calc_anchor;
        raop_rtp->anchor_rtp = rtp_time;
        raop_rtp->anchor_valid = true;
        raop_rtp->has_anchored = true;
        logger_log(raop_rtp->logger, LOGGER_INFO,
                   "raop_rtp_set_anchor: rate=1 anchor_rtp=%u anchor_local_ns=%llu",
                   rtp_time, (unsigned long long)calc_anchor);
    } else {
        raop_rtp->anchor_valid = false;
        logger_log(raop_rtp->logger, LOGGER_INFO, "raop_rtp_set_anchor: rate=0 (paused)");
    }
    MUTEX_UNLOCK(raop_rtp->run_mutex);
    if (do_flush && raop_rtp->callbacks.audio_flush) {
        raop_rtp->callbacks.audio_flush(raop_rtp->callbacks.cls);
    }
}

void
raop_rtp_flush_buffered(raop_rtp_t *raop_rtp, int64_t flush_from_seq, int64_t flush_until_seq, uint32_t flush_until_ts)
{
    if (!raop_rtp) return;
    MUTEX_LOCK(raop_rtp->run_mutex);
    raop_rtp->flush_from_seq = flush_from_seq;
    raop_rtp->flush_until_seq = flush_until_seq;
    raop_rtp->flush_until_ts = flush_until_ts;
    raop_rtp->buffered_flush_pending = true;
    MUTEX_UNLOCK(raop_rtp->run_mutex);
}

void raop_rtp_set_initial_sync(raop_rtp_t *raop_rtp) {
    if (!raop_rtp) return;
    raop_rtp->initial_sync = true;
    logger_log(raop_rtp->logger, LOGGER_INFO, "raop_rtp initial_sync unblocked for AirPlay 2 PTP");
}
'''

set_vol_target = 'void\nraop_rtp_set_volume(raop_rtp_t *raop_rtp, float volume)'
assert set_vol_target in rtp_c, "set_vol_target not found in raop_rtp.c"
rtp_c = rtp_c.replace(set_vol_target, buffered_code + '\n' + set_vol_target, 1)

# Close tsock and tcp_client_sock in raop_rtp_stop
stop_target = '''    if (raop_rtp->dsock != -1) {
        CLOSESOCKET(raop_rtp->dsock);
        raop_rtp->dsock = -1;
    }'''
stop_repl = '''    if (raop_rtp->dsock != -1) {
        CLOSESOCKET(raop_rtp->dsock);
        raop_rtp->dsock = -1;
    }
    if (raop_rtp->tsock != -1) {
        CLOSESOCKET(raop_rtp->tsock);
        raop_rtp->tsock = -1;
    }
    if (raop_rtp->tcp_client_sock != -1) {
        CLOSESOCKET(raop_rtp->tcp_client_sock);
        raop_rtp->tcp_client_sock = -1;
    }'''
assert stop_target in rtp_c, "stop_target not found in raop_rtp.c"
rtp_c = rtp_c.replace(stop_target, stop_repl, 1)

with open(rtp_c_path, 'w') as f:
    f.write(rtp_c)

# 4. raop_ntp.h and raop_ntp.c
ntp_h_path = f"{UX}/lib/raop_ntp.h"
with open(ntp_h_path, 'r') as f:
    ntp_h = f.read()
if 'raop_ntp_set_sync_offset' not in ntp_h:
    h_target = 'void  raop_ntp_set_video_arrival_offset(raop_ntp_t* raop_ntp, const uint64_t *offset);'
    h_repl = 'void raop_ntp_set_sync_offset(raop_ntp_t *raop_ntp, int64_t offset);\nvoid  raop_ntp_set_video_arrival_offset(raop_ntp_t* raop_ntp, const uint64_t *offset);'
    assert h_target in ntp_h, "h_target not found in raop_ntp.h"
    ntp_h = ntp_h.replace(h_target, h_repl, 1)
    with open(ntp_h_path, 'w') as f:
        f.write(ntp_h)

ntp_c_path = f"{UX}/lib/raop_ntp.c"
with open(ntp_c_path, 'r') as f:
    ntp_c = f.read()
if 'raop_ntp_set_sync_offset' not in ntp_c:
    c_target = 'void raop_ntp_set_video_arrival_offset(raop_ntp_t* raop_ntp, const uint64_t *offset) {'
    c_repl = """void raop_ntp_set_sync_offset(raop_ntp_t *raop_ntp, int64_t offset) {
    if (!raop_ntp) return;
    MUTEX_LOCK(raop_ntp->sync_params_mutex);
    raop_ntp->sync_offset = offset;
    raop_ntp->client_time_received = true;
    MUTEX_UNLOCK(raop_ntp->sync_params_mutex);
}

void raop_ntp_set_video_arrival_offset(raop_ntp_t* raop_ntp, const uint64_t *offset) {"""
    assert c_target in ntp_c, "c_target not found in raop_ntp.c"
    ntp_c = ntp_c.replace(c_target, c_repl, 1)
    with open(ntp_c_path, 'w') as f:
        f.write(ntp_c)

print('[*] Generating clean 0007-airplay2-multiroom-ptp.patch (relative to staged 0001-0006)...')
out_patch = f'{PATCHES}/0007-airplay2-multiroom-ptp.patch'
res = subprocess.run(['git', '-C', UX, 'diff'], capture_output=True, check=True)
with open(out_patch, 'wb') as f:
    f.write(res.stdout)

print(f'[+] Clean patch 0007 written ({len(res.stdout.splitlines())} lines)!')
