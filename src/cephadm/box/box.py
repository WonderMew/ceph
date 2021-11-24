#!/bin/python3
import sys
import argparse
import os
import stat
import host
import osd

from util import Config, run_shell_command, run_cephadm_shell_command, \
    ensure_inside_container, ensure_outside_container, get_boxes_container_info, \
    inside_container, get_host_ips, run_dc_shell_command



CEPH_IMAGE = 'quay.ceph.io/ceph-ci/ceph:master'
BOX_IMAGE = 'cephadm-box:latest'

def image_exists(image_name: str):
    # extract_tag
    assert image_name.find(':')
    image_name, tag = image_name.split(':')
    images = run_shell_command('docker image ls').split('\n')
    IMAGE_NAME = 0
    TAG = 1
    for image in images:
        image = image.split()
        print(image)
        print(image_name, tag)
        if image[IMAGE_NAME] == image_name and image[TAG] == tag:
            return True
    return False

def get_ceph_image():
    print('Getting ceph image')
    run_shell_command(f'docker pull {CEPH_IMAGE}')
    # update
    run_shell_command(f'docker build -t {CEPH_IMAGE} docker/ceph')
    if not os.path.exists('docker/ceph/image'):
        os.mkdir('docker/ceph/image')
    image_tar = 'docker/ceph/image/quay.ceph.image.tar'
    if not os.path.exists(image_tar):
        os.remove(image_tar)
    run_shell_command(f'docker save {CEPH_IMAGE} -o {image_tar}')
    print('Ceph image added')

def get_box_image():
    print('Getting box image')
    run_shell_command('docker build -t cephadm-box -f Dockerfile .')
    print('Box image added')

class Cluster:
    _help = 'Manage docker cephadm boxes'
    actions = ['bootstrap', 'start', 'down', 'list', 'sh', 'setup']
    parser = None

    @staticmethod
    def add_parser(subparsers):
        assert not Cluster.parser
        Cluster.parser = subparsers.add_parser('cluster', help=Cluster._help)
        parser = Cluster.parser
        parser.add_argument('action', choices=Cluster.actions, help='Action to perform on the box')
        parser.add_argument('--osds', type=int, default=1, help='Number of osds')
        parser.add_argument('--hosts', type=int, default=1, help='Number of hosts')
        parser.add_argument('--skip_deploy_osds', action='store_true', help='skip deploy osd')
        parser.add_argument('--skip_create_loop', action='store_true', help='skip create loopback device')

    def __init__(self, argv):
        self.argv = argv

    @ensure_outside_container
    def setup(self):
        get_ceph_image()
        get_box_image()

    @ensure_inside_container
    def bootstrap(self):
        print('Running bootstrap on seed')
        cephadm_path = os.environ.get('CEPHADM_PATH')
        os.symlink('/cephadm/cephadm', cephadm_path)
        st = os.stat(cephadm_path)
        os.chmod(cephadm_path, st.st_mode | stat.S_IEXEC)

        run_shell_command('docker load < /cephadm/box/docker/ceph/image/quay.ceph.image.tar')
        # cephadm guid error because it sometimes tries to use quay.ceph.io/ceph-ci/ceph:<none>
        # instead of master's tag
        run_shell_command('export CEPH_SOURCE_FOLDER=/ceph')
        run_shell_command('export CEPHADM_IMAGE=quay.ceph.io/ceph-ci/ceph:master')
        run_shell_command('echo "export CEPHADM_IMAGE=quay.ceph.io/ceph-ci/ceph:master" >> ~/.bashrc')

        extra_args = []

        shared_ceph_folder = os.environ.get('SHARED_CEPH_FOLDER')
        if shared_ceph_folder:
            extra_args.extend(['--shared_ceph_folder', 'shared_ceph_folder'])

        cephadm_image = os.environ.get('CEPHADM_IMAGE')
        if shared_ceph_folder:
            extra_args.append('--skip-pull')

        # cephadm prints in warning, let's redirect it to the output so shell_command doesn't
        # complain
        extra_args.append('2>&0')

        extra_args = ''.join(extra_args)

        cephadm_bootstrap_command = (
            '$CEPHADM_PATH --verbose bootstrap '
            '--mon-ip "$(hostname -i)" '
            '--allow-fqdn-hostname '
            '--initial-dashboard-password admin '
            '--dashboard-password-noupdate '
            '--shared_ceph_folder /ceph '
            f'{extra_args} '
        )

        print('Running cephadm bootstrap...')
        run_shell_command(cephadm_bootstrap_command)
        print('Cephadm bootstrap complete')


        run_shell_command('sudo vgchange --refresh')

        if not Config.get('skip_deploy_osds'):
            print('Deploying osds...')
            osds = Config.get('osds')
            for o in range(osds):
                osd.deploy_osd(f'/dev/vg1/lv{o}')
            print('Osds deployed')
        print('Bootstrap completed!')



    @ensure_outside_container
    def start(self):
        osds = Config.get('osds')
        hosts = Config.get('hosts')

        print('Checking docker images')
        if not image_exists(CEPH_IMAGE):
            get_ceph_image()
        if not image_exists(BOX_IMAGE):
            get_box_image()

        if not Config.get('skip_create_loop'):
            print('Adding logical volumes (block devices) in loopback device...')
            osd.create_loopback_devices(osds)
            print(f'Added {osds} logical volumes in a loopback device')

        print('Starting containers')
        # ensure boxes don't exist
        run_shell_command('docker-compose down')

        dcflags = '-f docker-compose.yml'
        if not os.path.exists('/sys/fs/cgroup/cgroup.controllers'):
            dcflags += ' -f docker-compose.cgroup1.yml'
        run_shell_command(f'docker-compose {dcflags} up --scale hosts={hosts} -d')

        run_shell_command('sudo sysctl net.ipv4.conf.all.forwarding=1')
        run_shell_command('sudo iptables -P FORWARD ACCEPT')

        print('Seting up host ssh servers')
        ips = get_host_ips()
        print(ips)
        for h in range(hosts):
            host._setup_ssh(h+1)

        verbose = '-v' if Config.get('verbose') else ''
        skip_deploy = '--skip_deploy_osds' if Config.get('skip_deploy_osds') else ''
        run_dc_shell_command(f'/cephadm/box/box.py {verbose} cluster bootstrap --osds {osds} --hosts {hosts} {skip_deploy}', 1, 'seed')

        host._copy_cluster_ssh_key(ips)

        print('Bootstrap finished successfully')

    @ensure_outside_container
    def down(self):
        run_shell_command('docker-compose down')
        print('Successfully killed all boxes')

    @ensure_outside_container
    def list(self):
        info = get_boxes_container_info()
        for container in info:
            print('\t'.join(container))

    @ensure_outside_container
    def sh(self):
        print('Seed bash')
        run_shell_command('docker-compose exec seed bash')

    def main(self):
        parser = Cluster.parser
        args = parser.parse_args(self.argv)
        Config.add_args(vars(args))
        function = getattr(self, args.action)
        function()


targets = {
    'cluster': Cluster,
    'osd': osd.Osd,
    'host': host.Host,
}

def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('-v', action='store_true', dest='verbose', help='be more verbose')

    subparsers = parser.add_subparsers()
    for _, target in targets.items():
        target.add_parser(subparsers)

    for count, arg in enumerate(sys.argv, 1):
        if arg in targets:
            instance = targets[arg](sys.argv[count:])
            if hasattr(instance, 'main'):
                args = parser.parse_args()
                Config.add_args(vars(args))
                instance.main()
                sys.exit(0)

    parser.print_help()

if __name__ == '__main__':
    main()
