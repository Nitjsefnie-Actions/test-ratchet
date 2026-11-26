"""The run action's plugin: conftest and collection paths, addopts' too, are the shard's."""
import os
import re
from importlib import import_module
from pathlib import Path


def pytest_addoption(parser):
    parser.addoption('--test-ratchet-shard', help='a file naming the shard test files, one a line')


def _kept(arguments, listing):
    files = set(Path(listing).read_text(encoding='utf-8').splitlines())
    return [argument for argument in arguments if argument in files]


# pytest's own is trylast.
def pytest_load_initial_conftests(early_config):
    namespace = early_config.known_args_namespace
    if namespace.test_ratchet_shard:
        namespace.file_or_dir[:] = _kept(namespace.file_or_dir, namespace.test_ratchet_shard)


def pytest_configure(config):
    listing = config.getoption('test_ratchet_shard')
    if listing:
        kept = _kept(config.args, listing)
        # The driver warns of those the command line named.
        ignored = [argument for argument in config.args
                   if argument not in kept and argument in config.invocation_params.args]
        if ignored:
            Path(listing + '.ignored').write_text(''.join(name + '\n' for name in ignored),
                                                  encoding='utf-8')
        config.args[:] = kept


def anchored(pattern, root):
    # prep_patterns reads others from cwd; bare names match anywhere.
    if pattern.startswith(('*', '?')) or not re.search(r'[\\/]', pattern):
        return pattern
    return import_module('coverage.files').abs_file(os.path.join(root, pattern))


# pytest-cov's hook, registered later, starts coverage first.
def pytest_sessionstart(session):
    if data := os.environ.get('COVERAGE_PROCESS_CONFIG'):
        config = import_module('coverage.config').CoverageConfig.deserialize(data)
        root = str(session.config.invocation_params.dir)
        config.run_omit = [anchored(pattern, root) for pattern in config.run_omit]
        config.run_include = [anchored(pattern, root) for pattern in config.run_include]
        os.environ['COVERAGE_PROCESS_CONFIG'] = config.serialize()


def pytest_sessionfinish(session):
    # A path repeated across roots is one name in coverage's XML.
    report = session.config.option.cov_report.get('xml')
    if report and Path(report).is_file():
        coverage = session.config.pluginmanager.getplugin('_cov').cov_controller.cov
        coverage.set_option('run:source', None)
        coverage.xml_report(outfile=report, ignore_errors=True)
