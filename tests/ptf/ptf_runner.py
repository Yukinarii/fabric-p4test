#!/usr/bin/env python2

# Copyright 2013-present Barefoot Networks, Inc.
# Copyright 2018-present Open Networking Foundation
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#   http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.
#

import argparse
import json
import logging
import os
import re
import struct
import subprocess
import sys
from collections import OrderedDict

import google.protobuf.text_format
import grpc
from p4.v1 import p4runtime_pb2
from p4.tmp import p4config_pb2

from bmv2 import Bmv2Switch

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger("PTF runner")


def error(msg, *args, **kwargs):
    logger.error(msg, *args, **kwargs)


def warn(msg, *args, **kwargs):
    logger.warn(msg, *args, **kwargs)


def info(msg, *args, **kwargs):
    logger.info(msg, *args, **kwargs)


def check_ifaces(ifaces):
    """
    Checks that required interfaces exist.
    """
    ifconfig_out = subprocess.check_output(['ifconfig'])
    iface_list = re.findall(r'^(\S+)', ifconfig_out, re.S | re.M)
    present_ifaces = set(iface_list)
    ifaces = set(ifaces)
    return ifaces <= present_ifaces


def build_bmv2_config(bmv2_json_path):
    """
    Builds the device config for BMv2
    """
    device_config = p4config_pb2.P4DeviceConfig()
    device_config.reassign = True
    with open(bmv2_json_path) as f:
        device_config.device_data = f.read()
    return device_config


def build_tofino_config(prog_name, bin_path, cxt_json_path):
    device_config = p4config_pb2.P4DeviceConfig()
    with open(bin_path, 'rb') as bin_f:
        with open(cxt_json_path, 'r') as cxt_json_f:
            device_config.device_data = ""
            device_config.device_data += struct.pack("<i", len(prog_name))
            device_config.device_data += prog_name
            tofino_bin = bin_f.read()
            device_config.device_data += struct.pack("<i", len(tofino_bin))
            device_config.device_data += tofino_bin
            cxt_json = cxt_json_f.read()
            device_config.device_data += struct.pack("<i", len(cxt_json))
            device_config.device_data += cxt_json
    return device_config


def update_config(p4info_path, bmv2_json_path, tofino_bin_path,
                  tofino_cxt_json_path, grpc_addr, device_id):
    """
    Performs a SetForwardingPipelineConfig on the device
    """
    channel = grpc.insecure_channel(grpc_addr)
    stub = p4runtime_pb2.P4RuntimeStub(channel)

    info("Sending P4 config")
    request = p4runtime_pb2.SetForwardingPipelineConfigRequest()
    request.device_id = device_id
    config = request.config
    with open(p4info_path, 'r') as p4info_f:
        google.protobuf.text_format.Merge(p4info_f.read(), config.p4info)
    if bmv2_json_path is not None:
        device_config = build_bmv2_config(bmv2_json_path)
    else:
        device_config = build_tofino_config("name", tofino_bin_path, tofino_cxt_json_path)
    config.p4_device_config = device_config.SerializeToString()
    request.action = p4runtime_pb2.SetForwardingPipelineConfigRequest.VERIFY_AND_COMMIT
    try:
        stub.SetForwardingPipelineConfig(request)
    except Exception as e:
        error("Error during SetForwardingPipelineConfig")
        error(str(e))
        return False
    return True


def run_test(p4info_path, grpc_addr, ptfdir, port_map_path, platform=None, extra_args=()):
    """
    Runs PTF tests included in provided directory.
    Device must be running and configfured with appropriate P4 program.
    """
    # TODO: check schema?
    # "ptf_port" is ignored for now, we assume that ports are provided by
    # increasing values of ptf_port, in the range [0, NUM_IFACES[.
    port_map = OrderedDict()
    with open(port_map_path, 'r') as port_map_f:
        port_list = json.load(port_map_f)
        for entry in port_list:
            p4_port = entry["p4_port"]
            iface_name = entry["iface_name"]
            port_map[p4_port] = iface_name

    if not check_ifaces(port_map.values()):
        error("Some interfaces are missing")
        return False

    ifaces = []
    # FIXME
    # find base_test.py
    pypath = os.path.dirname(os.path.abspath(__file__))
    if 'PYTHONPATH' in os.environ:
        os.environ['PYTHONPATH'] += ":" + pypath
    else:
        os.environ['PYTHONPATH'] = pypath
    for iface_idx, iface_name in port_map.items():
        ifaces.extend(['-i', '{}@{}'.format(iface_idx, iface_name)])
    cmd = ['ptf']
    cmd.extend(['--test-dir', ptfdir])
    cmd.extend(ifaces)
    test_params = 'p4info=\'{}\''.format(p4info_path)
    test_params += ';grpcaddr=\'{}\''.format(grpc_addr)
    if platform is not None:
        test_params += ';pltfm=\'{}\''.format(platform)
    cmd.append('--test-params={}'.format(test_params))
    cmd.extend(extra_args)
    info("Executing PTF command: {}".format(' '.join(cmd)))

    try:
        # we want the ptf output to be sent to stdout
        p = subprocess.Popen(cmd)
        p.wait()
    except:
        error("Error when running PTF tests")
        return False
    return p.returncode == 0


def check_ptf():
    try:
        with open(os.devnull, 'w') as devnull:
            subprocess.check_call(['ptf', '--version'],
                                  stdout=devnull, stderr=devnull)
        return True
    except subprocess.CalledProcessError:
        return True
    except OSError:  # PTF not found
        return False


# noinspection PyTypeChecker
def main():
    parser = argparse.ArgumentParser(
        description="Compile the provided P4 program and run PTF tests on it")
    parser.add_argument('--device',
                        help='Target device',
                        type=str, action="store", required=True,
                        choices=['tofino', 'bmv2'])
    parser.add_argument('--p4info',
                        help='Location of p4info proto in text format',
                        type=str, action="store", required=True)
    parser.add_argument('--bmv2-json',
                        help='Location BMv2 JSON output from p4c (if target is bmv2)',
                        type=str, action="store", required=False)
    parser.add_argument('--tofino-bin',
                        help='Location of Tofino .bin output from p4c (if target is tofino)',
                        type=str, action="store", required=False)
    parser.add_argument('--tofino-ctx-json',
                        help='Location of Tofino context.json output from p4c (if target is tofino)',
                        type=str, action="store", required=False)
    parser.add_argument('--grpc-addr',
                        help='Address to use to connect to P4 Runtime server',
                        type=str, default='localhost:50051')
    parser.add_argument('--device-id',
                        help='Device id for device under test',
                        type=int, default=0)
    parser.add_argument('--ptf-dir',
                        help='Directory containing PTF tests',
                        type=str, required=True)
    parser.add_argument('--port-map',
                        help='Path to JSON port mapping',
                        type=str, required=True)
    parser.add_argument('--platform',
                        help='Target platform on which tests are run (if target is tofino)',
                        type=str, required=False)
    parser.add_argument('--skip-config',
                        help='Assume a device with pipeline already configured',
                        action="store_true", default=False)
    parser.add_argument('--skip-test',
                        help='Skip test execution (useful to perform only pipeline configuration)',
                        action="store_true", default=False)
    args, unknown_args = parser.parse_known_args()

    if not check_ptf():
        error("Cannot find PTF executable")
        sys.exit(1)

    device = args.device
    bmv2_json = None
    tofino_ctx_json = None
    tofino_bin = None
    if not os.path.exists(args.p4info):
        error("P4Info file {} not found".format(args.p4info))
        sys.exit(1)
    if device == 'tofino':
        if not os.path.exists(args.tofino_bin):
            error("Tofino binary config file {} not found".format(args.tofino_bin))
            sys.exit(1)
        if not os.path.exists(args.tofino_ctx_json):
            error("Tofino context json file {} not found".format(args.tofino_ctx_json))
            sys.exit(1)
        tofino_bin = args.tofino_bin
        tofino_ctx_json = args.tofino_ctx_json
    elif device == 'bmv2':
        if not os.path.exists(args.bmv2_json):
            error("BMv2 json file {} not found".format(args.bmv2_json))
            sys.exit(1)
        bmv2_json = args.bmv2_json
    if not os.path.exists(args.port_map):
        print "Port map path '{}' does not exist".format(args.port_map)
        sys.exit(1)

    grpc_port = args.grpc_addr.split(':')[1]

    bmv2_sw = None
    if device == 'bmv2':
        bmv2_sw = Bmv2Switch(device_id=args.device_id,
                             port_map_path=args.port_map,
                             grpc_port=grpc_port,
                             cpu_port=255,
                             loglevel='debug')
        bmv2_sw.start()

    try:

        success = True

        if not args.skip_config:
            success = update_config(p4info_path=args.p4info,
                                    bmv2_json_path=bmv2_json,
                                    tofino_bin_path=tofino_bin,
                                    tofino_cxt_json_path=tofino_ctx_json,
                                    grpc_addr=args.grpc_addr,
                                    device_id=args.device_id)
        if not success:
            if bmv2_sw is not None:
                bmv2_sw.kill()
            sys.exit(2)

        if not args.skip_test:
            success = run_test(p4info_path=args.p4info,
                               grpc_addr=args.grpc_addr,
                               ptfdir=args.ptf_dir,
                               port_map_path=args.port_map,
                               platform=args.platform,
                               extra_args=unknown_args)

        if bmv2_sw is not None:
            bmv2_sw.kill()

        if not success:
            sys.exit(3)

    except Exception:
        if bmv2_sw is not None:
            bmv2_sw.kill()
        raise


if __name__ == '__main__':
    main()
