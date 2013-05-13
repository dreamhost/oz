# Copyright (C) 2011  Chris Lalancette <clalance@redhat.com>
# Copyright (C) 2012,2013  Chris Lalancette <clalancette@gmail.com>

# This library is free software; you can redistribute it and/or
# modify it under the terms of the GNU Lesser General Public
# License as published by the Free Software Foundation;
# version 2.1 of the License.

# This library is distributed in the hope that it will be useful,
# but WITHOUT ANY WARRANTY; without even the implied warranty of
# MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.  See the GNU
# Lesser General Public License for more details.

# You should have received a copy of the GNU Lesser General Public
# License along with this library; if not, write to the Free Software
# Foundation, Inc., 51 Franklin Street, Fifth Floor, Boston, MA  02110-1301 USA

"""
Debian installation
"""

import shutil
import os
import re

import oz.Guest
import oz.ozutil
import oz.OzException
import oz.linuxutil

class DebianGuest(oz.Guest.CDGuest):
    """
    Class for Debian 5, 6, and 7 installation.
    """
    def __init__(self, tdl, config, auto, output_disk, netdev, diskbus,
                 macaddress):
        oz.Guest.CDGuest.__init__(self, tdl, config, output_disk, netdev,
                                  None, None, diskbus, True, False, macaddress)

        self.ssh_startuplink = None
        self.cron_startuplink = None
        self.preseed_file = auto
        self.sshprivkey = os.path.join('/etc', 'oz', 'id_rsa-icicle-gen')
        self.crond_was_active = False
        self.sshd_was_active = False
        self.sshd_config = \
"""SyslogFacility AUTHPRIV
PasswordAuthentication yes
ChallengeResponseAuthentication no
GSSAPIAuthentication yes
GSSAPICleanupCredentials yes
UsePAM yes
AcceptEnv LANG LC_CTYPE LC_NUMERIC LC_TIME LC_COLLATE LC_MONETARY LC_MESSAGES
AcceptEnv LC_PAPER LC_NAME LC_ADDRESS LC_TELEPHONE LC_MEASUREMENT
AcceptEnv LC_IDENTIFICATION LC_ALL LANGUAGE
AcceptEnv XMODIFIERS
X11Forwarding yes
Subsystem       sftp    /usr/libexec/openssh/sftp-server
"""


        if self.preseed_file is None:
            self.preseed_file = oz.ozutil.generate_full_auto_path(
                                                              "debian-" +
                                                              self.tdl.update +
                                                              "-jeos.preseed"
                                                              )

    def _modify_iso(self):
        """
        Method to make the boot ISO auto-boot with appropriate parameters.
        """
        self.log.debug("Modifying ISO")

        self.log.debug("Copying preseed file")
        oz.ozutil.mkdir_p(os.path.join(self.iso_contents, "preseed"))

        outname = os.path.join(self.iso_contents, "preseed", "customiso.seed")

        if self.preseed_file == oz.ozutil.generate_full_auto_path(
                                                              "debian-" +
                                                              self.tdl.update +
                                                              "-jeos.preseed"):

            def _preseed_sub(line):
                """
                Method that is called back from oz.ozutil.copy_modify_file() to
                modify preseed files as appropriate for Debian.
                """
                if re.match('d-i passwd/root-password password', line):
                    return 'd-i passwd/root-password password ' +\
                                                    self.rootpw + '\n'
                elif re.match('d-i passwd/root-password-again password', line):
                    return 'd-i passwd/root-password-again password ' +\
                                                    self.rootpw + '\n'
                else:
                    return line

            oz.ozutil.copy_modify_file(self.preseed_file, outname,
                                       _preseed_sub)
        else:
            shutil.copy(self.preseed_file, outname)

        if self.tdl.arch == "x86_64":
            installdir = "/install.amd"
        else:
            # arch == i386
            installdir = "/install.386"

        self.log.debug("Modifying isolinux.cfg")
        isolinuxcfg = os.path.join(self.iso_contents, "isolinux",
                                   "isolinux.cfg")
        f = open(isolinuxcfg, 'w')
        f.write("default customiso\n")
        f.write("timeout 1\n")
        f.write("prompt 0\n")
        f.write("label customiso\n")
        f.write("  menu label ^Customiso\n")
        f.write("  menu default\n")
        f.write("  kernel " + installdir + "/vmlinuz\n")
        f.write("  append file=/cdrom/preseed/customiso.seed \
                DEBCONF_DEBUG=5 \
                debian-installer/locale=en_US \
                console-keymaps-at/keymap=\"American English\" \
                console-setup/layoutcode=us \
                netcfg/choose_interface=auto \
                auto=true \
                priority=critical initrd=" +
                installdir +
                "/initrd.gz --\n")
        f.close()

    def _generate_new_iso(self):
        """
        Method to create a new ISO based on the modified CD/DVD.
        """
        self.log.info("Generating new ISO")
        oz.ozutil.subprocess_check_output(["genisoimage", "-r", "-V", "Custom",
                                           "-J", "-l", "-no-emul-boot",
                                           "-b", "isolinux/isolinux.bin",
                                           "-c", "isolinux/boot.cat",
                                           "-boot-load-size", "4",
                                           "-cache-inodes", "-boot-info-table",
                                           "-v", "-v", "-o", self.output_iso,
                                           self.iso_contents])

    def _internal_customize(self, libvirt_xml, action):
        """
        Internal method to customize and optionally generate an ICICLE for the
        operating system after initial installation.
        """
        # the "action" input is actually a tri-state:
        # action = "gen_and_mod" means to generate the icicle and to
        #          potentially make modifications
        # action = "gen_only" means to generate the icicle only, and not
        #          look at any modifications
        # action = "mod_only" means to not generate the icicle, but still
        #          potentially make modifications

        self.log.info("Customizing image")

        if not self.tdl.packages and not self.tdl.files and not \
            self.tdl.commands and action == "mod_only":
            self.log.info("No additional packages, files, \
                          or commands to install, and icicle \
                          generation not requested, skipping customization")
            return
        # when doing an oz-install with -g, this isn't necessary as it will
        # just replace the port with the same port.  However, it is very
        # necessary when doing an oz-customize since the serial port might
        # not match what is specified in the libvirt XML
        modified_xml = self._modify_libvirt_xml_for_serial(libvirt_xml)

        self._collect_setup(modified_xml)

        icicle = None
        try:
            libvirt_dom = self.libvirt_conn.createXML(modified_xml, 0)

            try:
                guestaddr = None
                guestaddr = self._wait_for_guest_boot(libvirt_dom)
                self._test_ssh_connection(guestaddr)

                if action == "gen_and_mod":
                    self.do_customize(guestaddr)
                    icicle = self.do_icicle(guestaddr)
                elif action == "gen_only":
                    icicle = self.do_icicle(guestaddr)
                elif action == "mod_only":
                    self.do_customize(guestaddr)
                else:
                    raise oz.OzException.OzException(
                            "Invalid customize action %s; \
                             this is a programming error" % (action))
            finally:
                self._shutdown_guest(guestaddr, libvirt_dom)
        finally:
            self._collect_teardown(modified_xml)

        return icicle

    def _collect_setup(self, libvirt_xml):
        """
        Setup the guest for remote access.
        """
        self.log.info("Collection Setup")

        g_handle = self._guestfs_handle_setup(libvirt_xml)

        # we have to do 3 things to make sure we can ssh into Ubuntu
        # 1)  Upload our ssh key
        # 2)  Make sure sshd is running on boot
        # 3)  Make the guest announce itself to the host

        try:
            try:
                self._image_ssh_setup_step_1(g_handle)

                try:
                    self._image_ssh_setup_step_2(g_handle)

                    try:
                        self._image_ssh_setup_step_3(g_handle)

                    except:
                        self._image_ssh_teardown_step_3(g_handle)
                        raise
                except:
                    self._image_ssh_teardown_step_2(g_handle)
                    raise
            except:
                self._image_ssh_teardown_step_1(g_handle)
                raise

        finally:
            self._guestfs_handle_cleanup(g_handle)

    def _collect_teardown(self, libvirt_xml):
        """
        Method to reverse the changes done in _collect_setup.
        """
        self.log.info("Collection Teardown")

        g_handle = self._guestfs_handle_setup(libvirt_xml)

        try:
            self._image_ssh_teardown_step_1(g_handle)

            self._image_ssh_teardown_step_2(g_handle)

            self._image_ssh_teardown_step_3(g_handle)

            self._image_ssh_teardown_step_4(g_handle)
        finally:
            self._guestfs_handle_cleanup(g_handle)
            shutil.rmtree(self.icicle_tmp)

    def _get_service_runlevel_link(self, g_handle, service):
        """
        Method to find the runlevel link(s) for a service based on the name
        and the (detected) default runlevel.
        """
        runlevel = oz.linuxutil.get_default_runlevel(g_handle)

        lines = g_handle.cat('/etc/init.d/' + service).split("\n")
        startlevel = "99"
        for line in lines:
            if re.match('# chkconfig:', line):
                try:
                    startlevel = line.split(':')[1].split()[1]
                except:
                    pass
                break

        return "/etc/rc" + runlevel + ".d/S" + startlevel + service

    def _image_ssh_teardown_step_1(self, g_handle):
        """
        First step to undo _image_ssh_setup (remove authorized keys).
        """
        self.log.debug("Teardown step 1")
        # reset the authorized keys
        self.log.debug("Resetting authorized_keys")
        self._guestfs_path_restore(g_handle, '/root/.ssh')

    def _image_ssh_teardown_step_2(self, g_handle):
        """
        Second step to undo _image_ssh_setup (reset sshd service).
        """
        self.log.debug("Teardown step 2")
        # remove custom sshd_config
        self.log.debug("Resetting sshd_config")
        self._guestfs_path_restore(g_handle, '/etc/ssh/sshd_config')

        # reset the service link
        self.log.debug("Resetting sshd service")
        if self.ssh_startuplink:
            self._guestfs_path_restore(g_handle, self.ssh_startuplink)

    def _image_ssh_teardown_step_3(self, g_handle):
        """
        Fourth step to undo _image_ssh_setup (remove guest announcement).
        """
        self.log.debug("Teardown step 3")
        # remove announce cronjob
        self.log.debug("Resetting announcement to host")
        self._guestfs_remove_if_exists(g_handle, '/etc/cron.d/announce')

        # remove reportip
        self.log.debug("Removing reportip")
        self._guestfs_remove_if_exists(g_handle, '/root/reportip')

        # reset the service link
        self.log.debug("Resetting cron service")
        if self.cron_startuplink:
            self._guestfs_path_restore(g_handle, self.cron_startuplink)

    def _image_ssh_teardown_step_4(self, g_handle):
        """
        Fourth step to undo changes by the operating system.  For instance,
        during first boot openssh generates ssh host keys and stores them
        in /etc/ssh.  Since this image might be cached later on, this method
        removes those keys.
        """
        for f in ["/etc/ssh/ssh_host_dsa_key", "/etc/ssh/ssh_host_dsa_key.pub",
                  "/etc/ssh/ssh_host_rsa_key", "/etc/ssh/ssh_host_rsa_key.pub",
                  "/etc/ssh/ssh_host_ecdsa_key", "/etc/ssh/ssh_host_ecdsa_key.pub",
                  "/etc/ssh/ssh_host_key", "/etc/ssh/ssh_host_key.pub"]:
            self._guestfs_remove_if_exists(g_handle, f)

    def _image_ssh_setup_step_1(self, g_handle):
        """
        First step for allowing remote access (generate and upload ssh keys).
        """
        # part 1; upload the keys
        self.log.debug("Step 1: Uploading ssh keys")
        self._guestfs_path_backup(g_handle, '/root/.ssh')
        g_handle.mkdir('/root/.ssh')

        self._guestfs_path_backup(g_handle, '/root/.ssh/authorized_keys')

        self._generate_openssh_key(self.sshprivkey)

        g_handle.upload(self.sshprivkey + ".pub", '/root/.ssh/authorized_keys')

    def _image_ssh_setup_step_2(self, g_handle):
        """
        Second step for allowing remote access (configure sshd).
        """
        # part 2; check and setup sshd
        self.log.debug("Step 2: setup sshd")
        if not g_handle.exists('/usr/sbin/sshd'):
            raise oz.OzException.OzException("ssh not installed on the image, cannot continue")

        self.ssh_startuplink = self._get_service_runlevel_link(g_handle, 'ssh')
        self._guestfs_path_backup(g_handle, self.ssh_startuplink)
        g_handle.ln_sf('/etc/init.d/ssh', self.ssh_startuplink)

        sshd_config_file = os.path.join(self.icicle_tmp, "sshd_config")
        f = open(sshd_config_file, 'w')
        f.write(self.sshd_config)
        f.close()

        try:
            self._guestfs_path_backup(g_handle, '/etc/ssh/sshd_config')
            g_handle.upload(sshd_config_file, '/etc/ssh/sshd_config')
        finally:
            os.unlink(sshd_config_file)

    def _image_ssh_setup_step_3(self, g_handle):
        """
        Fourth step for allowing remote access (make the guest announce itself
        on bootup).
        """
        # part 3; make sure the guest announces itself
        self.log.debug("Step 3: Guest announcement")
        if not g_handle.exists('/usr/sbin/cron'):
            raise oz.OzException.OzException("cron not installed on the image, cannot continue")

        scriptfile = os.path.join(self.icicle_tmp, "script")
        f = open(scriptfile, 'w')
        f.write("#!/bin/bash\n")
        f.write("/bin/sleep 20\n")
        f.write("DEV=$(/usr/bin/awk '{if ($2 == 0) print $1}' /proc/net/route) &&\n")
        f.write('[ -z "$DEV" ] && exit 0\n')
        f.write("ADDR=$(/sbin/ip -4 -o addr show dev $DEV | /usr/bin/awk '{print $4}' | /usr/bin/cut -d/ -f1) &&\n")
        f.write('[ -z "$ADDR" ] && exit 0\n')
        f.write('echo -n "!$ADDR,%s!" > /dev/ttyS0\n' % (self.uuid))
        f.close()
        try:
            g_handle.upload(scriptfile, '/root/reportip')
            g_handle.chmod(0o755, '/root/reportip')
        finally:
            os.unlink(scriptfile)

        announcefile = os.path.join(self.icicle_tmp, "announce")
        f = open(announcefile, 'w')
        f.write('*/1 * * * * root /bin/bash -c "/root/reportip"\n')
        f.close()
        try:
            g_handle.upload(announcefile, '/etc/cron.d/announce')
        finally:
            os.unlink(announcefile)

        self.cron_startuplink = self._get_service_runlevel_link(g_handle,
                                                                'cron')
        self._guestfs_path_backup(g_handle, self.cron_startuplink)
        g_handle.ln_sf('/etc/init.d/cron', self.cron_startuplink)


    def _test_ssh_connection(self, guestaddr):
        """
        Internal method to test out the ssh connection before we try to use it.
        Under systemd, the IP address of a guest can come up and reportip can
        run before the ssh key is generated and sshd starts up.  This check makes
        sure that we allow an additional 30 seconds (1 second per ssh attempt)
        for sshd to finish initializing.
        """
        self.log.debug("Testing ssh connection")
        count = 30
        success = False
        while count > 0:
            try:
                stdout, stderr, retcode = self.guest_execute_command(guestaddr, 'ls', timeout=1)
                self.log.debug("Succeeded")
                success = True
                break
            except:
                count -= 1

        if not success:
            raise oz.OzException.OzException("Failed to connect to ssh on running guest")

    def customize(self, libvirt_xml):
        """
        Method to customize the operating system after installation.
        """
        return self._internal_customize(libvirt_xml, "mod_only")

    def _customize_repos(self, guestaddr):
        """
        Method to generate and upload custom repository files based on the TDL.
        """

        self.log.debug("Installing additional repository files")

        for repo in list(self.tdl.repositories.values()):
            self.guest_execute_command(guestaddr, "apt-add-repository '%s'" % (repo.url.strip('\'"')))
            self.guest_execute_command(guestaddr, "apt-get update")

    def do_customize(self, guestaddr):
        """
        Method to customize by installing additional packages and files.
        """
        if not self.tdl.packages and not \
            self.tdl.files and not \
            self.tdl.commands:
            # no work to do, just return
            return

        self._customize_repos(guestaddr)

        self.log.debug("Installing custom packages")
        packstr = ''
        for package in self.tdl.packages:
            packstr += package.name + ' '

        if packstr != '':
            self.guest_execute_command(guestaddr,
                                       'apt-get install -y %s' % (packstr),
                                       tunnels=None)

        self._customize_files(guestaddr)

        self.log.debug("Running custom commands")
        for cmd in self.tdl.commands:
            self.guest_execute_command(guestaddr, cmd)

    def guest_execute_command(self, guestaddr, command, timeout=10,
                              tunnels=None):
        """
        Method to execute a command on the guest and return the output.
        """
        return oz.ozutil.ssh_execute_command(guestaddr, self.sshprivkey,
                                             command, timeout, tunnels)


    def do_icicle(self, guestaddr):
        """
        Method to collect the package information and generate the ICICLE
        XML.
        """
        self.log.debug("Generating ICICLE")
        stdout, stderr, retcode = self.guest_execute_command(guestaddr,
                                                             'dpkg --get-selections',
                                                             timeout=30)

        # the data we get back from dpkg is in the form of:
        #
        # <package name>\t\t\tinstall
        #
        # so we have to strip out the tabs and the install before
        # passing it on to output_icicle_xml
        packages = []
        for line in stdout.split("\n"):
            packages.append(line.split("\t")[0])

        return self._output_icicle_xml(packages, self.tdl.description)

    def guest_live_upload(self, guestaddr, file_to_upload, destination,
                          timeout=10):
        """
        Method to copy a file to the live guest.
        """
        return oz.ozutil.scp_copy_file(guestaddr, self.sshprivkey,
                                       file_to_upload, destination, timeout)

    def _customize_files(self, guestaddr):
        """
        Method to upload the custom files specified in the TDL to the guest.
        """
        self.log.info("Uploading custom files")
        for name, content in list(self.tdl.files.items()):
            localname = os.path.join(self.icicle_tmp, "file")
            f = open(localname, 'w')
            f.write(content)
            f.close()
            try:
                self.guest_live_upload(guestaddr, localname, name)
            finally:
                os.unlink(localname)

    def _shutdown_guest(self, guestaddr, libvirt_dom):
        """
        Method to shutdown the guest (gracefully at first, then with prejudice).
        """
        if guestaddr is not None:
            try:
                self.guest_execute_command(guestaddr, 'shutdown -h now')
                if not self._wait_for_guest_shutdown(libvirt_dom):
                    self.log.warn("Guest did not shutdown in time, going to kill")
                else:
                    libvirt_dom = None
            except:
                self.log.warn("Failed shutting down guest, forcibly killing")

        if libvirt_dom is not None:
            try:
                libvirt_dom.destroy()
            except libvirt.libvirtError:
                # the destroy failed for some reason.  This can happen if
                # _wait_for_guest_shutdown times out, but the domain shuts
                # down before we get to destroy.  Check to make sure that the
                # domain is gone from the list of running domains; if so, just
                # continue on; if not, re-raise the error.
                for domid in self.libvirt_conn.listDomainsID():
                    if domid == libvirt_dom.ID():
                        raise


def get_class(tdl, config, auto, output_disk=None, netdev=None, diskbus=None,
              macaddress=None):
    """
    Factory method for Debian installs.
    """
    if tdl.update in ["5", "6", "7"]:
        if netdev is None:
            netdev = 'virtio'
        if diskbus is None:
            diskbus = 'virtio'
        return DebianGuest(tdl, config, auto, output_disk, netdev, diskbus,
                           macaddress)


def get_supported_string():
    """
    Return supported versions as a string.
    """
    return "Debian: 5, 6, 7"
