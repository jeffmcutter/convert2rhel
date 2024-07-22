import os
import re

import pexpect.exceptions
import pytest

from conftest import SYSTEM_RELEASE_ENV, TEST_VARS


PKI_ENTITLEMENT_CERTS_PATH = "/etc/pki/entitlement"

SERVER_SUB = "CentOS Linux"
PKGMANAGER = "yum"
FINAL_MESSAGE = "Diagnosis: There are no suitable mirrors available for the loaded repositories."

if "oracle" in SYSTEM_RELEASE_ENV:
    SERVER_SUB = "Oracle Linux Server"
elif "alma" in SYSTEM_RELEASE_ENV:
    SERVER_SUB = "AlmaLinux"
elif "rocky" in SYSTEM_RELEASE_ENV:
    SERVER_SUB = "Rocky Linux"
elif "stream" in SYSTEM_RELEASE_ENV:
    SERVER_SUB = "CentOS Stream"

if "8" in SYSTEM_RELEASE_ENV:
    PKGMANAGER = "dnf"


@pytest.fixture()
def yum_cache(shell):
    """
    We need to clean yum cache of packages and metadata downloaded by the
    previous test runs to correctly reproduce the transaction validation
    download fail.
    """
    assert shell("yum clean all --enablerepo=* --quiet").returncode == 0
    assert shell(f"rm -rf /var/cache/{PKGMANAGER}")


def remove_entitlement_certs():
    """
    Utility function to remove the entitlement certificate as soon as we
    notice it in the `PKI_ENTITLEMENT_CERTS_PATH`.

    We don't need to back it up and then restore it because the
    PKI_ENTITLEMENT_CERTS_PATH folder is only created during the conversion
    when the subscription-manager package is installed. And the .pem
    certificate is being generated by subscription-manager in the folder during
    the system registration. So to have the test system clean after the test
    finishes the certs shouldn't be present.
    """
    for cert_filename in os.listdir(PKI_ENTITLEMENT_CERTS_PATH):
        cert_path = os.path.join(PKI_ENTITLEMENT_CERTS_PATH, cert_filename)
        try:
            os.unlink(cert_path)
        except Exception as e:
            print("Failed to delete %s. Reason: %s" % (cert_path, e))


def test_package_download_error(convert2rhel, shell, yum_cache):
    """
    Remove the entitlement certs found at /etc/pki/entitlement during package
    download phase for both yum and dnf transactions.

    This will run the conversion up to the point where we validate the transaction.
    When the validation reaches a specific point, we remove the entitlement certs
    found in /etc/pki/entitlement/*.pem to ensure that the
    tool is doing a proper rollback when there is any failure during the package
    download.

    The package download happens in different phases for yum and dnf, yum
    downloads the packages during the `processTransaction` method call, while dnf
    has a specific method that processes and downloads the packages in the
    transaction.
    """
    with convert2rhel(
        "-y --serverurl {} --username {} --password {} --pool {} --debug".format(
            TEST_VARS["RHSM_SERVER_URL"],
            TEST_VARS["RHSM_USERNAME"],
            TEST_VARS["RHSM_PASSWORD"],
            TEST_VARS["RHSM_POOL"],
        )
    ) as c2r:
        c2r.expect("Validate the {} transaction".format(PKGMANAGER))
        c2r.expect("Adding {} packages to the {} transaction set.".format(SERVER_SUB, PKGMANAGER))

        if re.match(r"^(centos|oracle)-7$", SYSTEM_RELEASE_ENV):
            # Remove the repomd.xml for rhel-7-server-rpms repo
            os.unlink("/var/cache/yum/x86_64/7Server/rhel-7-server-rpms/repomd.xml")

        remove_entitlement_certs()

        # Error header first
        c2r.expect("Pre-conversion analysis report", timeout=600)
        c2r.expect("Must fix before conversion")
        if "8" in SYSTEM_RELEASE_ENV:
            # TODO
            # The second message should be in plural - FAILED_TO_DOWNLOAD_TRANSACTION_PACKAGES,
            # but the regex has troubles finding it, as it reports on another line
            c2r.expect("VALIDATE_PACKAGE_MANAGER_TRANSACTION::FAILED_TO_DOWNLOAD_TRANSACTION_PACKAGE")
        else:
            c2r.expect("VALIDATE_PACKAGE_MANAGER_TRANSACTION::FAILED_TO_LOAD_REPOSITORIES")

    assert c2r.exitstatus == 2


def test_transaction_validation_error(convert2rhel, shell, yum_cache):
    """
    Remove the entitlement certs found at /etc/pki/entitlement during transaction
    processing to throw the following yum error: pkgmanager.Errors.YumDownloadError

    This will run the conversion up to the point where we validate the transaction.
    When the validation reaches a specific point, we remove the entitlement certs
    found in /etc/pki/entitlement/*.pem to ensure that the
    tool is doing a proper rollback when the transaction is being processed.
    """
    with convert2rhel(
        "-y --serverurl {} --username {} --password {} --pool {} --debug".format(
            TEST_VARS["RHSM_SERVER_URL"],
            TEST_VARS["RHSM_USERNAME"],
            TEST_VARS["RHSM_PASSWORD"],
            TEST_VARS["RHSM_POOL"],
        )
    ) as c2r:
        c2r.expect(
            "Downloading and validating the yum transaction set, no modifications to the system will happen this time."
        )

        if re.match(r"^(centos|oracle)-7$", SYSTEM_RELEASE_ENV):
            # Remove the repomd.xml for rhel-7-server-rpms repo
            os.unlink("/var/cache/yum/x86_64/7Server/rhel-7-server-rpms/repomd.xml")

        remove_entitlement_certs()
        c2r.expect("Failed to validate the yum transaction.", timeout=600)

        # Error header first
        c2r.expect("Pre-conversion analysis report", timeout=600)
        c2r.expect("Must fix before conversion")
        c2r.expect_exact(
            "VALIDATE_PACKAGE_MANAGER_TRANSACTION::FAILED_TO_VALIDATE_TRANSACTION",
            timeout=600,
        )

    assert c2r.exitstatus == 2


@pytest.fixture
def packages_with_period(shell):
    """
    Fixture.
    Install problematic packages with period in name.
    E.g. python3.11-3.11.2-2.el8.x86_64 java-1.8.0-openjdk-headless-1.8.0.372.b07-4.el8.x86_64
    """
    problematic_packages = ["python3.11", "java-1.8.0-openjdk-headless"]

    # Install packages with in name period
    for package in problematic_packages:
        shell(f"yum install -y {package}")

    yield

    # Remove problematic packages
    for package in problematic_packages:
        shell(f"yum remove -y {package}")


def test_packages_with_in_name_period(shell, convert2rhel, packages_with_period):
    """
    This test verifies that packages with period in their name are parsed correctly.
        1/ Install problematic packages with period in name using packages_with_period fixture.
            E.g. python3.11-3.11.2-2.el8.x86_64 java-1.8.0-openjdk-headless-1.8.0.372.b07-4.el8.x86_64
        2/ Run conversion and expect no issues with the transaction validation.
            If there are issues with the Unhandled exception was caught: too many values to unpack (expected 2),
            raise AssertionError.
        3/ End the conversion at the Point of no return
    """

    with convert2rhel(
        "analyze --serverurl {} --username {} --password {} --pool {} --debug".format(
            TEST_VARS["RHSM_SERVER_URL"],
            TEST_VARS["RHSM_USERNAME"],
            TEST_VARS["RHSM_PASSWORD"],
            TEST_VARS["RHSM_POOL"],
        )
    ) as c2r:
        # Swallow the data collection warning
        assert c2r.expect("Prepare: Inform about data collection", timeout=300) == 0
        assert (
            c2r.expect(
                "The convert2rhel utility generates a /etc/rhsm/facts/convert2rhel.facts file that contains the below data about the system conversion.",
                timeout=300,
            )
            == 0
        )
        c2r.expect("Continue with the system conversion", timeout=300)
        c2r.sendline("y")

        c2r.expect("VALIDATE_PACKAGE_MANAGER_TRANSACTION has succeeded")

    assert c2r.exitstatus == 0


@pytest.mark.parametrize("yum_conf_exclude", [["redhat-release-server"]])
def test_override_exclude_list_in_yum_config(convert2rhel, kernel, yum_conf_exclude, shell):
    """
    This test verifies that packages that are defined in the exclude
    section in the /etc/yum.conf file are ignored during the analysis and
    conversion.
    The reason for us to ignore those packages, is that a user could
    specify something like 'redhat-release-server' in the exclude list, and
    that would cause dependency problems in the transaction.

        1/ Add the exclude section to /etc/yum.conf with the
            redhat-release-server package specified
        2/ Set the environment variable to skip kernel check
        3/ Boot into an older kernel
        4/ Run the analysis and check that the transaction was successful.
    """
    if os.environ["TMT_REBOOT_COUNT"] == "1":
        try:
            with convert2rhel(
                "analyze --serverurl {} --username {} --password {} --pool {} --debug -y".format(
                    TEST_VARS["RHSM_SERVER_URL"],
                    TEST_VARS["RHSM_USERNAME"],
                    TEST_VARS["RHSM_PASSWORD"],
                    TEST_VARS["RHSM_POOL"],
                )
            ) as c2r:
                c2r.expect("VALIDATE_PACKAGE_MANAGER_TRANSACTION has succeeded")

            assert c2r.exitstatus == 0
        except (AssertionError, pexpect.exceptions.EOF, pexpect.exceptions.TIMEOUT) as e:
            print(f"There was an error: \n{e}")
            shell(
                "tmt-report-result /tests/integration/tier0/non-destructive/single-yum-transaction-validation/override_exclude_list_in_yum_config FAIL"
            )
            raise
