#!/usr/bin/env python
# -*- coding: utf-8 -*-


import os
import base64
import boto3
import uuid
import s3keyring
from configparser import NoOptionError, NoSectionError, ConfigParser

from keyring.errors import (PasswordDeleteError, InitError)
from keyring.backend import KeyringBackend
from keyring.util.escape import escape as escape_for_s3


class PasswordGetError(Exception):
    """Raised when there is an error retrieving a password.
    """
    pass


class ConfigError(Exception):
    """Raised when the S3 backend has not been properly configured
    """


def supported():
    """Returns True if the S3 backed is supported on this system"""
    try:
        list(boto3.resource('s3').buckets.all())
        return True
    except:
        return False


class S3Backed(object):
    def __init__(self, kms_key_id=None, region=None, profile=None):
        """Creates a S3 bucket for the backend if one does not exist already"""
        self.__s3 = None
        self.__bucket = None
        self.__namespace = None
        self.__region = region
        self.__profile = profile
        self.__kms_key_id = kms_key_id

    @property
    def kms_key_id(self):
        if self.__kms_key_id is None:
            self.__kms_key_id = _get_config('aws', 'kms_key_id')
        return self.__kms_key_id

    @property
    def bucket(self):
        if self.__bucket is None:
            name = _get_config('aws', 'keyring_bucket', throw=False)
            self.__bucket = self._find_bucket(name)
        return self.__bucket

    @property
    def region(self):
        if self.__region is None:
            self.__region = s3keyring.read_config('aws', 'region')
        return self.__region

    @property
    def profile(self):
        if self.__profile is None:
            self.__profile = _get_config('aws', 'profile')
        return self.__profile

    @property
    def name(self):
        return self.bucket.name.split('keyring-')[1]

    @property
    def s3(self):
        if self.__s3 is None:
            self.__s3 = boto3.resource('s3')
        return self.__s3

    @property
    def namespace(self):
        """Namespaces allow you to have multiple keyrings backed by the same
        S3 bucket by separating them with different S3 prefixes. Different
        access permissions can then be given to different prefixes so that
        only the right IAM roles/users/groups have access to a keychain
        namespace"""
        if self.__namespace is None:
            self.__namespace = escape_for_s3(_get_config('aws',
                                                         'keyring_namespace'))

        return self.__namespace

    def _find_bucket(self, name=None):
        """Finds the backend S3 bucket. The backend bucket must be called
        keyring-[UUID].
        """
        if name is None:
            bucket = [b for b in self.s3.buckets.all()
                      if b.name.find('keyring-') == 0]
        else:
            bucket = [b for b in self.s3.buckets.all()
                      if b.name == name]
        if len(bucket) == 0:
            bucket_name = "keyring-{}".format(uuid.uuid4())
            bucket = self.s3.Bucket(bucket_name)
            bucket.create(ACL='private',
                          CreateBucketConfiguration={
                              'LocationConstraint': self.region})
        elif len(bucket) > 1:
            msg = ("Can't tell which of these buckets to use for the keyring: "
                   "{buckets}").format([b.name for b in bucket])
            raise InitError(msg)
        else:
            bucket = bucket[0]
        return bucket

    def _get_profile_default(self, profile, option):
        """Gets a default option value for a given AWS profile"""
        if profile not in self.config:
            profile = 'default'

        if option not in self.config[profile]:
            raise ConfigError("No default for option {} in profile {}".format(
                option, profile))

        return self.config[profile][option]


class S3Keyring(S3Backed, KeyringBackend):
    """
    BaseS3Keyring is a S3-based implementation of keyring.
    This keyring stores the password directly in S3 and provides methods
    which may be overridden by subclasses to support
    encryption and decryption. The encrypted payload is stored in base64
    format.
    """

    def _get_s3_key(self, service, username):
        """The S3 key where the secret will be stored"""
        return "{}/{}/{}/secret.b64".format(self.namespace, service, username)

    def get_password(self, service, username):
        """Read the password from the S3 bucket.
        """
        service = escape_for_s3(service)
        username = escape_for_s3(username)

        # Read the password from S3
        prefix = self._get_s3_key(service, username)
        values = list(self.bucket.objects.filter(Prefix=prefix))
        if len(values) == 0:
            # service/username not found
            return
        if len(values) > 1:
            msg = "Ambiguous prefix {prefix} in bucket {bucket}.".format(
                prefix=prefix, bucket=self.bucket.name)
            raise PasswordGetError(msg)
        pwd_base64 = values[0].get()['Body'].read()
        pwd = base64.decodestring(pwd_base64)
        return pwd.decode('utf-8')

    def set_password(self, service, username, password):
        """Write the password in the S3 bucket.
        """
        service = escape_for_s3(service)
        username = escape_for_s3(username)

        pwd_base64 = base64.encodestring(password.encode('utf-8')).decode()

        # Save in S3 using both server and client side encryption
        keyname = self._get_s3_key(service, username)
        self.bucket.Object(keyname).put(ACL='private', Body=pwd_base64,
                                        ServerSideEncryption='aws:kms',
                                        SSEKMSKeyId=self.kms_key_id)

    def delete_password(self, service, username):
        """Delete the password for the username of the service.
        """
        service = escape_for_s3(service)
        username = escape_for_s3(username)
        prefix = self._get_s3_key(service, username)
        objects = list(self.bucket.objects.filter(Prefix=prefix))
        if len(objects) == 0:
            msg = ("Password for service {service} and username {username} "
                   "not found.").format(service=service, username=username)
            raise PasswordDeleteError(msg)
        elif len(objects) > 1:
            msg = ("Multiple objects in bucket {bucket} match the prefix "
                   "{prefix}.").format(bucket=self.bucket.name,
                                       prefix=prefix)
        else:
            objects[0].delete()


def _get_config(section, option, throw=True):
    """Gets a configuration option or throws exception if not configured"""
    try:
        return s3keyring.read_config(section, option)
    except (NoOptionError, NoSectionError):
        if throw:
            raise InitError("You need to run: s3keyring configure")


def configure(ask=True):
    """Configures the keyring, requesting user input if necessary"""
    profile = _get_profile(ask=ask)
    if profile is None:
        profile_str = ''
    else:
        profile_str = profile
    s3keyring.write_config('aws', 'profile', profile_str)

    key_id = _get_aws_key('aws_access_key_id', profile, ask=ask)
    secret_key = _get_aws_key('aws_secret_access_key', profile, ask=ask)
    s3keyring.write_config('aws', 'access_key_id', key_id)
    s3keyring.write_config('aws', 'secret_access_key', secret_key)

    region = _get_region(profile, ask=ask)
    s3keyring.write_config('aws', 'region', region)

    fallback = {'keyring_namespace': 'default'}
    for option in ['kms_key_id', 'keyring_bucket', 'keyring_namespace']:
        value = _get_keyring_config(option, ask=ask, fallback=fallback)
        s3keyring.write_config('aws', option, value)

    # Make sure the configuration was correct
    check_config()


def check_config():
    """Checks that the configuration is not obviously wrong"""
    required = ['access_key_id', 'secret_access_key', 'kms_key_id', 'region',
                'keyring_bucket']
    for option in required:
        val = _get_config('aws', option, throw=False)
        if val is None or len(val) == 0:
            print("Warning: {} is required. You must run s3keyring "
                  "configure again.".format(option))


def _get_profile(ask=True):
    """Gets the AWS profile to use with s3keyring, if applicable"""
    profile = os.environ.get('AWS_PROFILE', None)
    if not profile:
        aws_creds = _get_aws_credentials()
        if aws_creds is None:
            # The AWS CLI is not configured: profile is N/A
            return
        profile = _get_config('aws', 'profile', throw=False)
    if profile is None:
        # Last resort, use the only profile in .aws/credentials
        cfg = _get_aws_credentials()
        if len(cfg.sections()) == 1:
            profile = cfg.sections()[0]

    resp = ''
    if ask:
        resp = input("AWS Profile to use [{}] : ".format(profile))
    if profile is None or len(resp) > 0:
        return resp

    return profile


def _get_aws_key(keyname, profile=None, ask=True):
    if profile is None:
        # We need to ask
        key = _guess_aws_key(keyname)
        if ask:
            tmp = input("AWS {keytitle} [{maskedkey}]: ".format(
                keytitle=keyname.replace('_', ' ').title(),
                maskedkey=_mask_key(key)))
            if len(tmp) > 0:
                # User wants to change the key
                key = tmp
    else:
        key = _get_aws_profile_key(keyname, profile)

    return key


def _guess_aws_key(keyname):
    key = s3keyring.read_config('aws', keyname.lower())
    if key == '':
        key = os.environ.get(keyname.upper(), '')
    return key


def _get_aws_profile_key(keyname, profile):
    cfg = _get_aws_credentials()
    return cfg.get(profile, keyname)


def _mask_key(key):
    if len(key) == 0:
        return key
    else:
        return '*'*(len(key) - 4) + key[-4:]


def _get_region(profile=None, ask=True):
    if profile is None:
        region = s3keyring.read_config('aws', 'region')
    else:
        cfg = _get_aws_config()
        if cfg and profile in cfg.sections() and \
                'region' in cfg.options(profile):
            region = cfg.get(profile, 'region')
        else:
            region = ''

    if region == '':
        region = os.environ.get('AWS_REGION', '')

    if ask:
        resp = input("AWS region [{}]: ".format(region))
        if len(resp) > 0:
            return resp

    return region


def _get_keyring_config(option, ask=True, fallback=None):
    val = s3keyring.read_config('aws', option.lower())
    if val == '':
        val = os.environ.get(option.upper(), '')
    if fallback and val == '':
        val = fallback.get(option.lower(), '')

    if ask:
        resp = input("{} [{}]: ".format(
            option.replace('_', ' ').title(), val))
        if len(resp) > 0:
            return resp

    return val


def _get_aws_config():
    cfg = ConfigParser()
    cfg_file = os.path.join(os.path.expanduser('~'), '.aws', 'config')
    if os.path.isfile(cfg_file):
        cfg.read(cfg_file)
        return cfg


def _get_aws_credentials():
    cfg = ConfigParser()
    cfg_file = os.path.join(os.path.expanduser('~'), '.aws', 'credentials')
    if os.path.isfile(cfg_file):
        cfg.read(cfg_file)
        return cfg
