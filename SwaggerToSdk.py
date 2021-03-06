"""Swagger to SDK"""
import platform
import shutil
import os
import stat
import subprocess
import logging
import tempfile
import argparse
import json
import zipfile
import re
from io import BytesIO
from pathlib import Path
from contextlib import contextmanager

import requests
from git import Repo, GitCommandError
from github import Github, GithubException

_LOGGER = logging.getLogger(__name__)

LATEST_TAG = 'latest'
AUTOREST_BASE_DOWNLOAD_LINK = "https://www.myget.org/F/autorest/api/v2/package/AutoRest/"

CONFIG_FILE = 'swagger_to_sdk_config.json'
NEEDS_MONO = platform.system() != 'Windows'

DEFAULT_BRANCH_NAME = 'autorest'
DEFAULT_TRAVIS_PR_BRANCH_NAME = 'RestAPI-PR{number}'
DEFAULT_TRAVIS_BRANCH_NAME = 'RestAPI-{branch}'
DEFAULT_COMMIT_MESSAGE = 'Generated from {hexsha}'

IS_TRAVIS = os.environ.get('TRAVIS') == 'true'

def get_documents_in_composite_file(composite_filepath):
    """Get the documents inside this composite file, relative to the repo root.

    :params str composite_filepath: The filepath, relative to the repo root or absolute.
    :returns: An iterable of Swagger specs in this composite file
    :rtype: list<str>"""
    pathconvert = lambda x: x.split('/master/')[1] if x.startswith('https') else x
    with composite_filepath.open() as composite_fd:
        return [pathconvert(d) for d in json.load(composite_fd)['documents']]

def find_composite_files(base_dir=Path('.')):
    """Find composite file.
    :rtype: pathlib.Path"""
    return list(Path(base_dir).glob('*/composite*.json'))

def swagger_index_from_composite(base_dir=Path('.')):
    """Build a reversed index of the composite files in thie repository.
    :rtype: dict"""
    return {
        doc: composite_file
        for composite_file in find_composite_files(base_dir)
        for doc in get_documents_in_composite_file(composite_file)
    }

def get_swagger_files_in_pr(pr_object):
    """Get the list of Swagger files in the given PR."""
    return {file.filename for file in pr_object.get_files()
            if re.match(r".*/swagger/.*\.json", file.filename, re.I)}

def get_swagger_project_files_in_pr(pr_object):
    """List project files in the PR, a project file being a Composite file or a Swagger file."""
    swagger_files_in_pr = get_swagger_files_in_pr(pr_object)
    swagger_index = swagger_index_from_composite()
    swagger_files_in_pr |= {str(swagger_index[s]).replace('\\','/')
                            for s in swagger_files_in_pr
                            if s in swagger_index}
    return swagger_files_in_pr


def read_config(sdk_git_folder, config_file):
    """Read the configuration file and return JSON"""
    config_path = os.path.join(sdk_git_folder, config_file)
    with open(config_path, 'r') as config_fd:
        return json.loads(config_fd.read())


def download_install_autorest(output_dir, autorest_version=LATEST_TAG):
    """Download and install Autorest in the given folder"""
    download_link = AUTOREST_BASE_DOWNLOAD_LINK
    if autorest_version != LATEST_TAG:
        download_link += autorest_version

    _LOGGER.info("Download Autorest from: %s", download_link)
    try:
        downloaded_package = requests.get(download_link)
    except:
        msg = "Unable to download Autorest for '{}', " \
                "please check this link and/or version tag: {}".format(
                    autorest_version,
                    download_link
                )
        _LOGGER.critical(msg)
        raise ValueError(msg)
    if downloaded_package.status_code != 200:
        raise ValueError(downloaded_package.content.decode())
    _LOGGER.info("Downloaded")
    with zipfile.ZipFile(BytesIO(downloaded_package.content)) as autorest_package:
        autorest_package.extractall(output_dir)
    return os.path.join(output_dir, 'tools', 'AutoRest.exe')

def merge_options(global_conf, local_conf, key):
    """Merge the conf using override: local conf is prioritary over global"""
    global_keyed_conf = global_conf.get(key) # Could be None
    local_keyed_conf = local_conf.get(key) # Could be None

    if global_keyed_conf is None or local_keyed_conf is None:
        return global_keyed_conf or local_keyed_conf

    if isinstance(global_keyed_conf, list):
        options = set(global_keyed_conf)
    else:
        options = dict(global_keyed_conf)

    options.update(local_keyed_conf)
    return options

def build_autorest_options(language, global_conf, local_conf):
    """Build the string of the Autorest options"""
    merged_options = merge_options(global_conf, local_conf, "autorest_options") or {}

    if "CodeGenerator" not in merged_options:
        merged_options["CodeGenerator"] = "Azure.{}".format(language)

    sorted_keys = sorted(list(merged_options.keys())) # To be honest, just to help for tests...
    return " ".join("-{} {}".format(key, str(merged_options[key])) for key in sorted_keys)

def generate_code(language, swagger_file, output_dir, autorest_exe_path, global_conf=None, local_conf=None):
    """Call the Autorest process with the given parameters"""
    if NEEDS_MONO:
        autorest_exe_path = 'mono ' + autorest_exe_path

    autorest_options = build_autorest_options(language, global_conf, local_conf)

    cmd_line = "{} -i {} -o {} {}"
    cmd_line = cmd_line.format(autorest_exe_path,
                               swagger_file,
                               output_dir,
                               autorest_options)
    _LOGGER.info("Autorest cmd line:\n%s", cmd_line)

    try:
        result = subprocess.check_output(cmd_line.split(),
                                         stderr=subprocess.STDOUT,
                                         universal_newlines=True)
    except subprocess.CalledProcessError as err:
        _LOGGER.error(err)
        _LOGGER.error(err.output)
        raise
    except Exception as err:
        _LOGGER.error(err)
        raise
    else:
        _LOGGER.info(result)


def get_swagger_hexsha(restapi_git_folder):
    """Get the SHA1 of the current repo"""
    repo = Repo(restapi_git_folder)
    if repo.bare:
        not_git_hexsha = "notgitrepo"
        _LOGGER.warning("Not a git repo, SHA1 used will be: %s", not_git_hexsha)
        return not_git_hexsha
    hexsha = repo.head.commit.hexsha
    _LOGGER.info("Found REST API repo SHA1: %s", hexsha)
    return hexsha


def update(generated_folder, destination_folder, global_conf, local_conf):
    """Update data from generated to final folder"""
    wrapper_files_or_dirs = merge_options(global_conf, local_conf, "wrapper_filesOrDirs") or []
    delete_files_or_dirs = merge_options(global_conf, local_conf, "delete_filesOrDirs") or []
    generated_relative_base_directory = local_conf.get('generated_relative_base_directory') or \
        global_conf.get('generated_relative_base_directory')

    client_generated_path = Path(generated_folder)
    if generated_relative_base_directory:
        client_generated_path = next(client_generated_path.glob(generated_relative_base_directory))

    for wrapper_file_or_dir in wrapper_files_or_dirs:
        for file_path in Path(destination_folder).glob(wrapper_file_or_dir):
            relative_file_path = file_path.relative_to(destination_folder)
            file_path_dest = client_generated_path.joinpath(str(relative_file_path))
            file_path.replace(file_path_dest)

    for delete_file_or_dir in delete_files_or_dirs:
        for file_path in client_generated_path.glob(delete_file_or_dir):
            if file_path.is_file():
                file_path.unlink()
            else:
                shutil.rmtree(str(file_path))

    shutil.rmtree(destination_folder)
    client_generated_path.replace(destination_folder)

def checkout_and_create_branch(repo, name):
    """Checkout branch. Create it if necessary"""
    local_branch = repo.branches[name] if name in repo.branches else None
    if not local_branch:
        if name in repo.remotes.origin.refs:
            # If origin branch exists but not local, git.checkout is the fatest way
            # to create local branch with origin link automatically
            msg = repo.git.checkout(name)
            _LOGGER.debug(msg)
            return
        # Create local branch, will be link to origin later
        local_branch = repo.create_head(name)
    local_branch.checkout()


def compute_branch_name(branch_name, gh_token=None):
    """Compute the branch name depended on Travis, default or not"""
    if branch_name:
        return branch_name
    if not IS_TRAVIS:
        return DEFAULT_BRANCH_NAME
    _LOGGER.info("Travis detected")
    pr_object = get_initial_pr(gh_token)
    if not pr_object:
        return DEFAULT_TRAVIS_BRANCH_NAME.format(branch=os.environ['TRAVIS_BRANCH'])
    return DEFAULT_TRAVIS_PR_BRANCH_NAME.format(number=pr_object.number)


def do_commit(repo, message_template, branch_name, hexsha):
    "Do a commit if modified/untracked files"
    repo.git.add(repo.working_tree_dir)

    if not repo.git.diff(staged=True):
        _LOGGER.warning('No modified files in this Autorest run')
        return False

    checkout_and_create_branch(repo, branch_name)
    msg = message_template.format(hexsha=hexsha)
    repo.index.commit(msg)
    _LOGGER.info("Commit done: %s", msg)
    return True


def do_pr(gh_token, sdk_git_id, sdk_pr_target_repo_id, branch_name, base_branch):
    "Do the PR"
    if not gh_token:
        _LOGGER.info('Skipping the PR, no token found')
        return
    if not sdk_pr_target_repo_id:
        _LOGGER.info('Skipping the PR, no target repo id')
        return

    github_con = Github(gh_token)
    sdk_pr_target_repo = github_con.get_repo(sdk_pr_target_repo_id)

    if '/' in sdk_git_id:
        sdk_git_owner = sdk_git_id.split('/')[0]
        _LOGGER.info("Do the PR from %s", sdk_git_owner)
        head_name = "{}:{}".format(sdk_git_owner, branch_name)
    else:
        head_name = branch_name

    body = ''
    rest_api_pr = get_initial_pr(gh_token)
    if rest_api_pr:
        body += "Generated from RestAPI PR: {}".format(rest_api_pr.html_url)
    try:
        github_pr = sdk_pr_target_repo.create_pull(
            title='Automatic PR from {}'.format(branch_name),
            body=body,
            head=head_name,
            base=base_branch
        )
    except GithubException as err:
        if err.status == 422 and err.data['errors'][0]['message'].startswith('A pull request already exists'):
            _LOGGER.info('PR already exists, it was a commit on an open PR')
            return
        raise
    _LOGGER.info("Made PR %s", github_pr.html_url)
    comment = compute_pr_comment_with_sdk_pr(github_pr.html_url, sdk_git_id, branch_name)
    add_comment_to_initial_pr(gh_token, comment)


def get_pr_object_from_travis(gh_token=None):
    """If Travis, return the Github object representing the PR.
       If result is None, is not Travis.
       The GH token is optional if the repo is public.
    """
    if not IS_TRAVIS:
        return
    pr_number = os.environ['TRAVIS_PULL_REQUEST']
    if pr_number == 'false':
        _LOGGER.info("This build don't come from a PR")
        return
    github_con = Github(gh_token)
    github_repo = github_con.get_repo(os.environ['TRAVIS_REPO_SLUG'])

    return github_repo.get_pull(int(pr_number))

def compute_pr_comment_with_sdk_pr(comment, sdk_fork_id, branch_name):
    travis_string = "[![Build Status]"\
                        "(https://travis-ci.org/{fork_repo_id}.svg?branch={branch_name})]"\
                        "(https://travis-ci.org/{fork_repo_id})"
    travis_string = travis_string.format(branch_name=branch_name,
                                         fork_repo_id=sdk_fork_id)
    return travis_string+' '+comment

def get_pr_from_travis_commit_sha(gh_token=None):
    """Try to determine the initial PR using #<number> in the current commit comment.
    Will check if the found number is really a merged PR.
    The GH token is optional if the repo is public."""
    if not IS_TRAVIS:
        return
    github_con = Github(gh_token)
    github_repo = github_con.get_repo(os.environ['TRAVIS_REPO_SLUG'])

    local_commit = github_repo.get_commit(os.environ['TRAVIS_COMMIT'])
    commit_message = local_commit.commit.message
    issues_in_message = re.findall('#([\\d]+)', commit_message)

    issue_object = None
    for issue in issues_in_message:
        try:
            _LOGGER.info('Check if %s is a PR', issue)
            issue_object = github_repo.get_pull(int(issue))
            if not issue_object.is_merged():
                continue
            break
        except Exception as err:
            pass
    if not issue_object:
        _LOGGER.warning('Was not able to found PR commit message')
    return issue_object

def get_initial_pr(gh_token=None):
    """Try to deduce the initial PR of the current repo state.
    Use Travis env variable first, try with commit regexp otherwise.
    gh_token could be None for public repo.

    :param str gh_token: A Github token. Useful only if the repo is private.
    :return: A PR object if found, None otherwise
    :rtype: github.PullRequest.PullRequest
    """
    return get_pr_object_from_travis(gh_token) or \
        get_pr_from_travis_commit_sha(gh_token)

def add_comment_to_initial_pr(gh_token, comment):
    """Add a comment to the initial PR.
    :returns: True is comment added, False if PR not found"""
    if not gh_token:
        return False
    initial_pr = get_initial_pr(gh_token)
    if not initial_pr:
        return False
    initial_pr.create_issue_comment(comment)
    return True

def configure_user(gh_token, repo):
    """git config --global user.email "you@example.com"
       git config --global user.name "Your Name"
    """
    user = user_from_token(gh_token)
    repo.git.config('user.email', user.email or 'autorestci@microsoft.com')
    repo.git.config('user.name', user.name or 'SwaggerToSDK Automation')

def user_from_token(gh_token):
    """Get user login from GitHub token"""
    github_con = Github(gh_token)
    return github_con.get_user()

def sync_fork(gh_token, github_repo_id, repo):
    """Sync the current branch in this fork against the direct parent on Github"""
    if not gh_token:
        _LOGGER.warning('Skipping the upstream repo sync, no token')
        return
    _LOGGER.info('Check if repo has to be sync with upstream')
    github_con = Github(gh_token)
    github_repo = github_con.get_repo(github_repo_id)

    upstream_url = 'https://github.com/{}.git'.format(github_repo.parent.full_name)
    upstream = repo.create_remote('upstream', url=upstream_url)
    upstream.fetch()
    active_branch_name = repo.active_branch.name
    if not active_branch_name in repo.remotes.upstream.refs:
        _LOGGER.info('Upstream has no branch %s to merge from', active_branch_name)
        return
    else:
        _LOGGER.info('Merge from upstream')
    msg = repo.git.rebase('upstream/{}'.format(repo.active_branch.name))
    _LOGGER.debug(msg)
    msg = repo.git.push()
    _LOGGER.debug(msg)


def get_full_sdk_id(gh_token, sdk_git_id):
    """If the SDK git id is incomplete, try to complete it with user login"""
    if not '/' in sdk_git_id:
        login = user_from_token(gh_token).login
        return '{}/{}'.format(login, sdk_git_id)
    return sdk_git_id

def clone_to_path(gh_token, temp_dir, sdk_git_id):
    """Clone the given repo_id to the 'sdk' folder in given temp_dir"""
    _LOGGER.info("Clone SDK repository %s", sdk_git_id)

    credentials_part = ''
    if gh_token:
        login = user_from_token(gh_token).login
        credentials_part = '{user}:{token}@'.format(
            user=login,
            token=gh_token
        )
    else:
        _LOGGER.warning('Will clone the repo without writing credentials')

    https_authenticated_url = 'https://{credentials}github.com/{sdk_git_id}.git'.format(
        credentials=credentials_part,
        sdk_git_id=sdk_git_id
    )
    sdk_path = os.path.join(temp_dir, 'sdk')
    Repo.clone_from(https_authenticated_url, sdk_path)
    _LOGGER.info("Clone success")

    return sdk_path

def remove_readonly(func, path, _):
    "Clear the readonly bit and reattempt the removal"
    os.chmod(path, stat.S_IWRITE)
    func(path)

@contextmanager
def manage_sdk_folder(gh_token, temp_dir, sdk_git_id):
    """Context manager to avoid readonly problem while cleanup the temp dir"""
    sdk_path = clone_to_path(gh_token, temp_dir, sdk_git_id)
    _LOGGER.debug("SDK path %s", sdk_path)
    try:
        yield sdk_path
        # Pre-cleanup for Windows http://bugs.python.org/issue26660
    finally:
        _LOGGER.debug("Preclean SDK folder")
        shutil.rmtree(sdk_path, onerror=remove_readonly)

def install_autorest(temp_dir, global_conf=None, autorest_dir=None):
    """ Return an AutoRest.exe path.
    Either download using temp_dir and conf, either check presence in
    autorest_dir. IF autorest_dir is provided, AutoRest.exe must be found inside.
    """
    if autorest_dir:
        autorest_path = Path(autorest_dir, 'AutoRest.exe')
        if autorest_path.exists():
            return str(autorest_path)
        raise ValueError('{} does not exists'.format(autorest_path))

    if global_conf is None:
        global_conf = {}
    autorest_version = global_conf.get("autorest", LATEST_TAG)

    autorest_temp_dir = os.path.join(temp_dir, 'autorest')
    os.mkdir(autorest_temp_dir)

    return download_install_autorest(autorest_temp_dir, autorest_version)


def build_libraries(gh_token, config_path, project_pattern, restapi_git_folder,
         sdk_git_id, pr_repo_id, message_template, base_branch_name, branch_name,
         autorest_dir=None):
    """Main method of the the file"""
    sdk_git_id = get_full_sdk_id(gh_token, sdk_git_id)

    with tempfile.TemporaryDirectory() as temp_dir, \
            manage_sdk_folder(gh_token, temp_dir, sdk_git_id) as sdk_folder:

        sdk_repo = Repo(sdk_folder)
        if gh_token:
            branch_name = compute_branch_name(branch_name, gh_token)
            _LOGGER.info('Destination branch for generated code is %s', branch_name)
            configure_user(gh_token, sdk_repo)
            try:
                _LOGGER.info('Try to checkout the destination branch if it already exists')
                sdk_repo.git.checkout(branch_name)
            except GitCommandError:
                _LOGGER.info('Destination branch does not exists')
                sdk_repo.git.checkout(base_branch_name)
            sync_fork(gh_token, sdk_git_id, sdk_repo)
        else:
            _LOGGER.info('No token provided, simply checkout base branch')
            sdk_repo.git.checkout(base_branch_name)

        config = read_config(sdk_repo.working_tree_dir, config_path)

        global_conf = config["meta"]
        language = global_conf["language"]
        hexsha = get_swagger_hexsha(restapi_git_folder)

        initial_pr = get_initial_pr(gh_token)
        swagger_files_in_pr = get_swagger_project_files_in_pr(initial_pr) if initial_pr else set()

        autorest_exe_path = install_autorest(temp_dir, global_conf, autorest_dir)

        for project, local_conf in config["projects"].items():
            if project_pattern and not any(project.startswith(p) for p in project_pattern):
                _LOGGER.info("Skip project %s", project)
                continue

            if initial_pr and local_conf['swagger'] not in swagger_files_in_pr:
                _LOGGER.info("Skip file not in PR %s", project)
                continue

            _LOGGER.info("Working on %s", local_conf['swagger'])
            dest = local_conf['output_dir']
            swagger_file = os.path.join(restapi_git_folder, local_conf['swagger'])

            if not os.path.isfile(swagger_file):
                err_msg = "Swagger file does not exist or is not readable: {}".format(
                    swagger_file)
                _LOGGER.critical(err_msg)
                raise ValueError(err_msg)

            dest_folder = os.path.join(sdk_repo.working_tree_dir, dest)
            if not os.path.isdir(dest_folder):
                err_msg = "Dest folder does not exist or is not accessible: {}".format(
                    dest_folder)
                _LOGGER.critical(err_msg)
                raise ValueError(err_msg)

            generated_path = os.path.join(temp_dir, os.path.basename(swagger_file))
            generate_code(language,
                          swagger_file, generated_path,
                          autorest_exe_path, global_conf, local_conf)
            update(generated_path, dest_folder, global_conf, local_conf)

        if gh_token:
            if do_commit(sdk_repo, message_template, branch_name, hexsha):
                sdk_repo.git.push('origin', branch_name, set_upstream=True)
                if pr_repo_id:
                    do_pr(gh_token, sdk_git_id, pr_repo_id, branch_name, base_branch_name)
            else:
                add_comment_to_initial_pr(gh_token, "No modification for {}".format(language))
        else:
            _LOGGER.warning('Skipping commit creation since no token is provided')

    _LOGGER.info("Build SDK finished and cleaned")


def main():
    """Main method"""
    epilog = "\n".join([
        'The script activates this additional behaviour if Travis is detected:',
        ' --branch is setted by default to "{}" if triggered by a PR, "{}" otherwise'.format(
            DEFAULT_TRAVIS_PR_BRANCH_NAME,
            DEFAULT_TRAVIS_BRANCH_NAME
        ),
        ' Only the files inside the PR are considered. If the PR is NOT detected, all files are used.'
    ])

    parser = argparse.ArgumentParser(
        description='Build SDK using Autorest and push to Github. The GH_TOKEN environment variable needs to be set to act on Github.',
        epilog=epilog,
        formatter_class=argparse.RawTextHelpFormatter)
    parser.add_argument('--rest-folder', '-r',
                        dest='restapi_git_folder', default='.',
                        help='Rest API git folder. [default: %(default)s]')
    parser.add_argument('--pr-repo-id',
                        dest='pr_repo_id', default=None,
                        help='PR repo id. If not provided, no PR is done')
    parser.add_argument('--message', '-m',
                        dest='message', default=DEFAULT_COMMIT_MESSAGE,
                        help='Force commit message. {hexsha} will be the current REST SHA1 [default: %(default)s]')
    parser.add_argument('--project', '-p',
                        dest='project', action='append',
                        help='Select a specific project. Do all by default. You can use a substring for several projects.')
    parser.add_argument('--base-branch', '-o',
                        dest='base_branch', default='master',
                        help='The base branch from where create the new branch and where to do the final PR. [default: %(default)s]')
    parser.add_argument('--branch', '-b',
                        dest='branch', default=None,
                        help='The SDK branch to commit. Default if not Travis: {}. If Travis is detected, see epilog for details'.format(DEFAULT_BRANCH_NAME))
    parser.add_argument('--config', '-c',
                        dest='config_path', default=CONFIG_FILE,
                        help='The JSON configuration format path [default: %(default)s]')
    parser.add_argument('--autorest',
                        dest='autorest_dir',
                        help='Force the Autorest to be executed. Must be a directory containing Autorest.exe')
    parser.add_argument("-v", "--verbose",
                        dest="verbose", action="store_true",
                        help="Verbosity in INFO mode")
    parser.add_argument("--debug",
                        dest="debug", action="store_true",
                        help="Verbosity in DEBUG mode")

    parser.add_argument('sdk_git_id',
                        help='The SDK Github id. '\
                         'If a simple string, consider it belongs to the GH_TOKEN owner repo. '\
                         'Otherwise, you can use the syntax username/repoid')

    args = parser.parse_args()

    if 'GH_TOKEN' not in os.environ:
        gh_token = None
    else:
        gh_token = os.environ['GH_TOKEN']

    main_logger = logging.getLogger()
    if args.verbose or args.debug:
        logging.basicConfig()
        main_logger.setLevel(logging.DEBUG if args.debug else logging.INFO)

    build_libraries(gh_token,
                    args.config_path, args.project,
                    args.restapi_git_folder, args.sdk_git_id,
                    args.pr_repo_id,
                    args.message, args.base_branch, args.branch,
                    args.autorest_dir)

if __name__ == "__main__":
    main()
