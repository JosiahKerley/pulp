from gettext import gettext as _
import logging
import os
import re
import time

import pkg_resources

from mongoengine.queryset import DoesNotExist

from pulp.common.compat import iter_modules
from pulp.server.db.model import MigrationTracker
import pulp.server.db.migrations


_logger = logging.getLogger(__name__)


MIGRATIONS_ENTRY_POINT = 'pulp.server.db.migrations'


class MigrationRemovedError(Exception):
    def __init__(self, migration_version, component_version, min_component_version,
                 component='pulp'):
        """
        :param migration_version:       version number of the current migration
        :type  migration_version:       str
        :param component_version:       version of the component in which a migration was removed
        :type  component_version:       str
        :param min_component_version:   minimum version of the component that has all of the
                                        removed migrations
        :type  min_component_version:   str
        :param component:               name of the component which is being migrated, such as
                                        "pulp", "pulp_rpm", etc. Default is "pulp"
        :type  component:               str
        """
        self.migration_version = migration_version
        self.component_version = component_version
        self.min_component_version = min_component_version
        self.component = component

        message_args = {
            'm': migration_version,
            'current': component_version,
            'min': min_component_version,
            'component': self.component
        }
        self.message = _('Migrations %(m)s and earlier were removed in %(component)s %(current)s. '
                         'Please upgrade to at least %(component)s %(min)s before attempting to '
                         'upgrade to %(current)s or greater.') % message_args
        super(MigrationRemovedError, self).__init__()

    def __str__(self):
        return 'MigrationRemovedError: %s' % self.message

    def __repr__(self):
        return 'MigrationRemovedError("%s", "%s", "%s", "%s")' % (self.migration_version,
                                                                  self.component_version,
                                                                  self.min_component_version,
                                                                  self.component)


class MigrationModule(object):
    """
    This is a wrapper around the real migration module. It allows us to add a version attribute to
    the module without interfering with the module's namespace, and it also natively sorts by
    migration version. It has a reference to the module's migrate() function as its migrate
    attribute.
    """
    class MissingMigrate(Exception):
        """
        This is raised when something attempts to instantiate a MigrationModule with a module that
        has no migrate() function.
        """
        pass

    class MissingVersion(Exception):
        """
        This is raised when something attempts to instantiate a MigrationModule with a module that
        does not conform to the standard version naming conventions.
        """
        pass

    def __init__(self, python_module_name):
        """
        Initialize a MigrationModule to represent the module passed in by python_module_name.

        :param python_module_name: The full python module name in dotted notation.
        :type  python_module_name: str
        """
        self._module = _import_all_the_way(python_module_name)
        self.version = self._get_version()
        if not hasattr(self._module, 'migrate'):
            raise self.__class__.MissingMigrate()
        self.migrate = self._module.migrate

    @property
    def name(self):
        """
        We use self._module.__name__ as self.name for convenience.
        """
        return self._module.__name__

    def _get_version(self):
        """
        Parse the module's name with a regex to determine the version of the module. The module is
        expected to be named something along the lines of ####<name>.py. We don't care how many
        digits are used to represent the number, but we do expect it to be the beginning of the
        name, and we do expect a trailing underscore.

        :returns:   The version of the module
        :rtype:     int
        """
        migration_module_name = self.name.split('.')[-1]
        version_match = re.match(r'^(?P<version>\d+).*', migration_module_name)
        if not version_match:
            # If the version regex doesn't match, this is not a module that follows our naming
            # convention
            raise self.__class__.MissingVersion()
        version = int(version_match.groupdict()['version'])
        return version

    def __cmp__(self, other_module):
        """
        This is used to get MigrationModules to sort by their version.

        :returns: A negative value if self's version is less that other's, 0 if they are equal,
                  and a positive value if self's version is greater than other's.
        :rtype:   int
        """
        return cmp(self.version, other_module.version)

    def __repr__(self):
        return str(self)

    def __str__(self):
        return str(self.name)


class MigrationPackage(object):
    """
    A wrapper around the migration packages found in pulp.server.db.migrations. Has methods to
    retrieve information about the migrations that are found there, and to apply the migrations.
    """
    class DuplicateVersions(Exception):
        """
        This is raised when a single migration package has two MigrationModules in it that have the
        same version.
        """
        pass

    def __init__(self, python_package):
        """
        Initialize the MigrationPackage to represent the Python migration package passed in.

        :param python_package: The Python package this object should represent
        :type  python_package: package
        """
        self._package = python_package
        self.allow_fast_forward = getattr(python_package, 'allow_fast_forward', False)
        self.duration = None

        try:
            self._migration_tracker = MigrationTracker.objects().get(name=self.name)
        except DoesNotExist:
            self._migration_tracker = MigrationTracker(name=self.name)
            self._migration_tracker.save()

        # Calculate the latest available version
        available_versions = self.available_versions
        if available_versions:
            self.latest_available_version = available_versions[-1]
        else:
            self.latest_available_version = 0

    def apply_migration(self, migration, update_current_version=True):
        """
        Apply the migration that is passed in, and update the DB to note the new version that this
        migration represents.

        :param migration:              The migration to apply
        :type  migration:              pulp.server.db.migrate.utils.MigrationModule
        :param update_current_version: If True, update the package's current version after
                                       successful application
                                       If False, don't update.
        :type  update_current_version: bool
        """
        start = time.time()
        migration.migrate()
        if update_current_version:
            self._migration_tracker.version = migration.version
            self._migration_tracker.save()
        self.duration = time.time() - start

    @property
    def available_versions(self):
        """
        Return a list of the migration versions that are available in this migration package.

        :rtype: list
        """
        migrations = self.migrations
        versions = [migration.version for migration in migrations]
        return versions

    @property
    def current_version(self):
        """
        An integer that represents the migration version that the database is currently at.
        None means that the migration package has never been run before.

        :rtype: int
        """
        return self._migration_tracker.version

    @property
    def migrations(self):
        """
        Finds all available migration modules for the MigrationPackage,
        and then sorts by the version. Return a list of MigrationModules.

        :rtype: list
        """
        # Generate a list of the names of the modules found inside this package
        module_names = [name for module_loader, name, ispkg in
                        iter_modules([os.path.dirname(self._package.__file__)])]
        migration_modules = []
        for module_name in module_names:
            try:
                module_name = '%s.%s' % (self.name, module_name)
                migration_modules.append(MigrationModule(module_name))
            except MigrationModule.MissingMigrate:
                msg = _("The module %(m)s doesn't have a migrate function. It will be ignored.")
                msg = msg % {'m': module_name}
                _logger.debug(msg)
            except MigrationModule.MissingVersion:
                msg = _("The module %(m)s doesn't conform to the migration package naming "
                        "conventions. It will be ignored.")
                msg = msg % {'m': module_name}
                _logger.debug(msg)
        migration_modules.sort()
        last_version = 0
        for module in migration_modules:
            if module.version == 0:
                error_message = _('0 is a reserved migration version number, but the '
                                  'module %(n)s has been assigned that version.')
                error_message = error_message % {'n': module.name}
                raise self.__class__.DuplicateVersions(error_message)
            if module.version == last_version:
                error_message = _('There are two migration modules that share version %(v)s in '
                                  '%(n)s.')
                error_message = error_message % {'v': module.version, 'n': self.name}
                raise self.__class__.DuplicateVersions(error_message)
            last_version = module.version
        return migration_modules

    @property
    def name(self):
        """
        Returns the name of the Python package that this MigrationPackage represents.

        :rtype: str
        """
        return self._package.__name__

    @property
    def unapplied_migrations(self):
        """
        Return a list of MigrationModules in this package that have not been applied yet.

        :rtype: list
        """
        return [migration for migration in self.migrations
                if migration.version > self.current_version]

    def __cmp__(self, other_package):
        """
        This method returns a negative value if self.name < other_package.name, 0 if they are
        equal, and a positive value if self.name > other_package.name. There is an exception to this
        sorting rule, in that if self._package is pulp.server.db.migrations, this method will always
        return -1, and if other_package is pulp.server.db.migrations, it will always return 1.

        :rtype: int
        """
        if self._package is pulp.server.db.migrations:
            return -1
        if other_package._package is pulp.server.db.migrations:
            return 1
        return cmp(self.name, other_package.name)

    def __str__(self):
        return self.name

    def __repr__(self):
        return str(self)


def check_package_versions():
    """
    Inspects each migration package returned by get_migration_packages(), and makes sure they have
    each been migrated to the latest version. If they have not, it logs the issue and raises an
    Exception.
    """
    errors = []
    for package in get_migration_packages():
        if package.current_version != package.latest_available_version:
            error_message = _("%(p)s hasn't been updated to the latest available migration.")
            error_message = error_message % {'p': package.name}
            _logger.error(error_message)
            errors.append(error_message)
    if errors:
        error_message = _("There are unapplied migrations. Please run the database management "
                          "utility to apply them.")
        raise Exception(error_message)


def get_migration_packages():
    """
    This method finds and returns a list of MigrationPackages. The MigrationPackages are found by
    using pkg_resources to find Python packages that use the pulp.server.db.migrations entry point.
    The official Pulp platform migrations are also included in the list. It sorts them
    alphabetically by name, except that pulp.server.db.migrations unconditionally sorts to the front
    of the list. Returns a list of MigrationPackages.

    :rtype: list
    """
    migration_packages = [MigrationPackage(pulp.server.db.migrations)]
    for entry_point in pkg_resources.iter_entry_points(MIGRATIONS_ENTRY_POINT):
        try:
            migration_package_module = entry_point.load()
            migration_packages.append(MigrationPackage(migration_package_module))
        except MigrationPackage.DuplicateVersions as e:
            _logger.error(str(e))
    migration_packages.sort()
    return migration_packages


def _import_all_the_way(module_string):
    """
    The __import__ method returns the top level module when asked for a module with the dotted
    notation. For example, __import__('a.b.c') will return a, not c. This is fine, but we could
    use something that returns c for our migration discovery code. That's what this does.

    :param module_string: A dot notation of the Python module to be imported and returned
    :type  module_string: str
    :rtype:               module
    """
    module = __import__(module_string)
    parts_to_import = module_string.split('.')
    parts_to_import.pop(0)
    for part in parts_to_import:
        module = getattr(module, part)
    return module
