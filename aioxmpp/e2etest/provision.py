########################################################################
# File name: provision.py
# This file is part of: aioxmpp
#
# LICENSE
#
# This program is free software: you can redistribute it and/or modify
# it under the terms of the GNU Lesser General Public License as
# published by the Free Software Foundation, either version 3 of the
# License, or (at your option) any later version.
#
# This program is distributed in the hope that it will be useful, but
# WITHOUT ANY WARRANTY; without even the implied warranty of
# MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.  See the GNU
# Lesser General Public License for more details.
#
# You should have received a copy of the GNU Lesser General Public
# License along with this program.  If not, see
# <http://www.gnu.org/licenses/>.
#
########################################################################
import abc
import ast
import asyncio
import enum
import fnmatch
import json
import logging

import aioxmpp
import aioxmpp.disco
import aioxmpp.security_layer
import aioxmpp.connector


_logger = logging.getLogger(__name__)


class Quirk(enum.Enum):
    """
    Enumeration of implementation quirks.

    Each enumeration member represents a quirk of an implementation. A quirk is
    a behaviour of an implementation which does not directly violate standards,
    but which is unfortunate in a way that it disables some features of
    :mod:`aioxmpp`.

    One example of such a quirk is the rewriting of message stanza IDs which
    some MUC implementations do when reflecting the messages. This breaks the
    stanza tracking of :meth:`aioxmpp.muc.Room.send_tracked_message`.

    The following quirks are defined:

    .. attribute:: MUC_REWRITES_MESSAGE_ID
       :annotation: https://zombofant.net/xmlns/aioxmpp/e2etest/quirks#muc-id-rewrite

       This quirk must be configured when the environment the provisioner
       provides rewrites the message IDs when they are reflected by the MUC
       implementation.

       The quirk does not need to be set if the environment does not provide a
       MUC implementation at all.
    """

    MUC_REWRITES_MESSAGE_ID = \
        "https://zombofant.net/xmlns/aioxmpp/e2etest/quirks#muc-id-rewrite"
    NO_ADHOC_PING = \
        "https://zombofant.net/xmlns/aioxmpp/e2etest/quirks#no-adhoc-ping"


def fix_quirk_str(s):
    if s.startswith("#"):
        return "https://zombofant.net/xmlns/aioxmpp/e2etest/quirks" + s
    return s


def configure_tls_config(section):
    """
    Generate keyword arguments for use with :meth:`.security_layer.make` from
    the configuration which control the TLS behaviour of the security layer.

    :param section: Configuration section to work on.
    :return: Keyword arguments for :meth:`.security_layer.make`
    :rtype: :class:`dict`

    The generated keyword arguments are ``pin_type``, ``pin_store`` and
    ``no_verify``. The options in the config file have the same names and the
    semantics are the following:

    ``pin_store`` and ``pin_type`` can be used to configure certificate
    pinning, in case the server you want to test against does not have a
    certificate which passes the default OpenSSL PKIX tests.

    If set, ``pin_store`` must point to a JSON file, which consists of a single
    object mapping host names to arrays of strings containing the base64
    representation of what is being pinned. This is determined by ``pin_type``,
    which can be ``0`` for Public Key pinning and ``1`` for Certificate
    pinning.

    There is also the ``no_verify`` option, which, if set to true, will disable
    certificate verification altogether. This does not much harm if you are
    testing against localhost anyways and saves the configuration nuisance for
    certificate pinning. ``no_verfiy`` takes precedence over ``pin_store`` and
    ``pin_type``.
    """

    no_verify = section.getboolean(
        "no_verify",
        fallback=False
    )

    if not no_verify and "pin_store" in section:
        with open(section.get("pin_store")) as f:
            pin_store = json.load(f)
        pin_type = aioxmpp.security_layer.PinType(
            section.getint("pin_type", fallback=0)
        )
    else:
        pin_store = None
        pin_type = None

    return {
        "pin_store": pin_store,
        "pin_type": pin_type,
        "no_verify": no_verify,
    }


def configure_quirks(section):
    """
    Generate a set of :class:`.Quirk` enum members from the given configuration
    section.

    :param section: Configuration section to work on.
    :return: Set of :class:`.Quirk` members

    This parses the configuration key ``quirks`` as a python literal (see
    :func:`ast.literal_eval`). It expects a list of strings as a result.

    The strings are interpreted as :class:`.Quirk` enum values. If a string
    starts with ``#``, it is prefixed with
    ``https://zombofant.net/xmlns/aioxmpp/e2etest/quirks`` for easier manual
    writing of the configuration. See :class:`.Quirk` for the currently defined
    quirks.
    """

    quirks = ast.literal_eval(section.get("quirks", fallback="[]"))
    if isinstance(quirks, (str, dict)):
        raise ValueError("incorrect type for quirks setting")
    return set(map(Quirk, map(fix_quirk_str, quirks)))


def configure_blockmap(section):
    blockmap_raw = ast.literal_eval(section.get("block_features",
                                                fallback="{}"))
    return {
        aioxmpp.JID.fromstr(entity): features
        for entity, features in blockmap_raw.items()
    }


def _is_feature_blocked(peer, feature, blockmap):
    return any(
        fnmatch.fnmatch(feature, item)
        for item in blockmap.get(peer, [])
    )


@asyncio.coroutine
def discover_server_features(disco, peer, recurse_into_items=True,
                             blockmap={}):
    """
    Use :xep:`30` service discovery to discover features supported by the
    server.

    :param disco: Service discovery client which can query the `peer` server.
    :type disco: :class:`aioxmpp.DiscoClient`
    :param peer: The JID of the server to query
    :type peer: :class:`~aioxmpp.JID`
    :param recurse_into_items: If set to true, the :xep:`30` items exposed by
                               the server will also be queried for their
                               features. Only one level of recursion is
                               performed.
    :return: A mapping which maps :xep:`30` feature vars to the JIDs at which
             the service is provided.

    This uses :xep:`30` service discovery to obtain a set of features supported
    at `peer`. The set of features is returned as a mapping which maps the
    ``var`` values of the features to the JID at which they were discovered.

    If `recurse_into_items` is true, a :xep:`30` items query is run against
    `peer`. For each JID discovered that way, :func:`discover_server_features`
    is re-invoked (with `recurse_into_items` set to false). The resulting
    mappings are merged with the mapping obtained from querying the features of
    `peer` (existing entries are *not* overriden -- so `peer` takes
    precedence).
    """

    server_info = yield from disco.query_info(peer)

    all_features = {
        feature: [peer]
        for feature in server_info.features
        if not _is_feature_blocked(peer, feature, blockmap)
    }

    if recurse_into_items:
        server_items = yield from disco.query_items(peer)
        features_list = yield from asyncio.gather(
            *(
                discover_server_features(
                    disco,
                    item.jid,
                    recurse_into_items=False,
                )
                for item in server_items.items
                if item.jid is not None and item.node is None
            )
        )

        for features in features_list:
            for feature, providers in features.items():
                all_features.setdefault(feature, []).extend(providers)

    return all_features


class Provisioner(metaclass=abc.ABCMeta):
    """
    Base class for provisioners.

    Provisioners are responsible for providing test cases with XMPP accounts
    and client objects connected to these accounts, as well as information
    about the environment the accounts live in.

    A provisioner must implement the following methods:

    .. automethod:: _make_client

    .. automethod:: configure

    The following methods are the API used by test cases:

    .. automethod:: get_connected_client

    .. automethod:: get_feature_provider

    .. automethod:: has_quirk

    These methods can be used by provisioners to perform plumbing tasks, such
    as shutting down clients or deleting accounts:

    .. automethod:: initialise

    .. automethod:: finalise

    .. automethod:: setup

    .. automethod:: teardown

    """

    def __init__(self, logger=_logger):
        super().__init__()
        self._accounts_to_dispose = []
        self._featuremap = {}
        self._account_info = None
        self._logger = logger
        self.__counter = 0

    @abc.abstractmethod
    @asyncio.coroutine
    def _make_client(self, logger):
        """
        :param logger: The logger to pass to the client.
        :return: Client with a fresh account.

        Construct a new :class:`aioxmpp.PresenceManagedClient` connected to a
        new account. This method must be re-implemented by subclasses.
        """

    @asyncio.coroutine
    def get_connected_client(self, presence=aioxmpp.PresenceState(True), *,
                             services=[], prepare=None):
        """
        Return a connected client to a unique XMPP account.

        :param presence: initial presence to emit
        :type presence: :class:`aioxmpp.PresenceState`
        :param prepare: a coroutine run after the services
            are summoned but before the client connects.
        :type prepare: coroutine receiving the client
             as argument
        :raise OSError: if the connection failed
        :raise RuntimeError: if a client could not be provisioned due to
                             resource constraints
        :return: Connected presence managed client
        :rtype: :class:`aioxmpp.PresenceManagedClient`

        Each account used by the clients returned from this method is unique;
        all clients are guaranteed to have different bare JIDs.

        The clients and accounts are cleaned up after the tear down of the test
        runs. Some provisioners may have a limit on the number of accounts
        which can be used in the same test.

        Clients obtained from this function are cleaned up automatically on
        tear down of the test. The clients are stopped and the accounts
        deleted or cleared, so that each test starts with a fully fresh state.

        A coroutine may be passed as `prepare` argument. It is called
        with the client as the single argument after all services in
        `services` have been summoned but before the client connects,
        this is for example useful to connect signals that fire early
        in the connection process.
        """
        id_ = self.__counter
        self.__counter += 1
        self._logger.debug("obtaining client%d from %r", id_, self)
        logger = self._logger.getChild("client{}".format(id_))
        client = yield from self._make_client(logger)
        for service in services:
            client.summon(service)
        if prepare is not None:
            yield from prepare(client)
        cm = client.connected(presence=presence)
        yield from cm.__aenter__()
        self._accounts_to_dispose.append(cm)
        return client

    def get_feature_providers(self, feature_nses):
        """
        :param feature_ns: Namespace URIs to find a provider for
        :type feature_ns: iterable of :class:`str`
        :return: JIDs of the entities providing all features
        :rtype: :class:`set` of :class:`aioxmpp.JID`

        If there is no entity supporting all requested features, the empty set
        is returned.
        """
        providers = set()
        iterator = iter(feature_nses)
        try:
            first_ns = next(iterator)
        except StopIteration:
            return None

        providers = set(self._featuremap.get(first_ns, []))
        for feature_ns in iterator:
            providers &= set(self._featuremap.get(feature_ns, []))
        return providers

    def get_feature_provider(self, feature_nses):
        """
        :param feature_ns: Namespace URIs to find a provider for
        :type feature_ns: iterable of :class:`str`
        :return: JID of the entity providing all features
        :rtype: :class:`aioxmpp.JID`

        If there is no entity supporting all requested features, :data:`None`
        is returned.
        """
        providers = self.get_feature_providers(feature_nses)
        if not providers:
            return None
        return next(iter(providers))

    def get_feature_subset_provider(self, feature_nses, required_subset):
        required_subset = set(required_subset)

        candidates = {}
        for feature_ns in feature_nses:
            providers = self._featuremap.get(feature_ns, [])
            for provider in providers:
                candidates.setdefault(provider, set()).add(feature_ns)

        candidates = sorted(
            (
                (provider, features)
                for provider, features in candidates.items()
                if features & required_subset == required_subset
            ),
            key=lambda x: (len(x[1]))
        )

        try:
            return candidates.pop()
        except IndexError:
            return None, None

    def has_quirk(self, quirk):
        """
        :param quirk: Quirk to check for
        :type quirk: :class:`Quirk`
        :return: true if the environment has the given quirk
        """
        return quirk in self._quirks

    def has_pep(self):
        """
        :return: true if the account has PEP support, false otherwise.
        """
        if not self._account_info:
            return False
        return any(ident.category == "pubsub" and ident.type_ == "pep"
                   for ident in self._account_info.identities)

    @abc.abstractmethod
    def configure(self, section):
        """
        Read the configuration and set up the provisioner.

        :param section: mapping of config keys to values

        Subclasses will implement this to configure their account setup and
        servers to use.

        .. seealso::
           :func:`configure_tls_config`
              for a function which extracts TLS-related arguments for
              :func:`aioxmpp.security_layer.make`
           :func:`configure_quirks`
              for a function which extracts a set of :class:`.Quirk`
              enumeration members from the configuration
           :func:`configure_blockmap`
              for a function which extracts a mapping which allows to block
              features from specific hosts
        """

    @asyncio.coroutine
    def initialise(self):
        """
        Called once on test framework startup.

        Subclasses may run service discovery code here to detect features of
        the environment they are connected to.

        .. seealso::

           :func:`discover_server_features`
              for a function which uses :xep:`30` service discovery to find
              features.
        """

    @asyncio.coroutine
    def finalise(self):
        """
        Called once on test framework shutdown (timeout of 10 seconds applies).
        """

    @asyncio.coroutine
    def setup(self):
        """
        Called before each test run.
        """

    @asyncio.coroutine
    def teardown(self):
        """
        Called after each test run.

        The default implementation cleans up the clients obtained from
        :meth:`get_connected_client`.
        """

        futures = []
        for cm in self._accounts_to_dispose:
            futures.append(asyncio.async(cm.__aexit__(None, None, None)))
        self._accounts_to_dispose.clear()

        self._logger.debug("waiting for %d accounts to shut down",
                           len(futures))
        yield from asyncio.gather(
            *futures,
            return_exceptions=True
        )


class AnonymousProvisioner(Provisioner):
    """
    This provisioner uses SASL ANONYMOUS to obtain accounts.

    It is dead-simple to configure: it needs a host to connect to, and
    optionally some TLS and quirks configuration. The host is specified as
    configuration key ``host``, TLS can be configured as documented in
    :func:`configure_tls_config` and quirks are set as described in
    :func:`configure_quirks`. A configuration for a locally running Prosody
    instance might look like this:

    .. code-block:: ini

       [aioxmpp.e2etest.provision.AnonymousProvisioner]
       host=localhost
       no_verify=true
       quirks=[]

    The server configured in ``host`` must support SASL ANONYMOUS and must
    allow communication between the clients connected that way. It may provide
    PubSub and/or MUC services, which will be auto-discovered if they are
    provided in the :xep:`30` items of the server.

    .. note::

       Make sure to disable PEP (:xep:`163`) support on the server, to avoid
       the ``…pubsub#publish`` feature to be bound to the server instead of the
       pubsub component.

       This is unfortunate because it prohibits testing PEP properly. This may
       be fixed in a future release when anything PEP-specific is implemented.
    """

    def configure(self, section):
        super().configure(section)
        self.__host = section.get("host")
        self.__domain = aioxmpp.JID.fromstr(section.get(
            "domain",
            self.__host
        ))
        self.__port = section.getint("port")
        self.__security_layer = aioxmpp.make_security_layer(
            None,
            anonymous="",
            **configure_tls_config(
                section
            )
        )
        self.__blockmap = configure_blockmap(section)
        self._quirks = configure_quirks(section)

    @asyncio.coroutine
    def _make_client(self, logger):
        override_peer = []
        if self.__port is not None:
            override_peer.append(
                (self.__host, self.__port,
                 aioxmpp.connector.STARTTLSConnector())
            )

        return aioxmpp.PresenceManagedClient(
            self.__domain,
            self.__security_layer,
            override_peer=override_peer,
            logger=logger,
        )

    @asyncio.coroutine
    def initialise(self):
        self._logger.debug("initialising anonymous provisioner")

        client = yield from self.get_connected_client()
        disco = client.summon(aioxmpp.DiscoClient)

        self._featuremap.update(
            (yield from discover_server_features(
                disco,
                self.__domain,
                blockmap=self.__blockmap,
            ))
        )

        self._logger.debug("found %d features", len(self._featuremap))
        if self._logger.isEnabledFor(logging.DEBUG):
            for feature, providers in self._featuremap.items():
                self._logger.debug(
                    "%s provided by %s",
                    feature,
                    ", ".join(sorted(map(str, providers)))
                )

        self._account_info = yield from disco.query_info(None)

        # clean up state
        del client
        yield from self.teardown()
