import os
import json
import logging
from typing import Any, Dict

from pygit2 import (
    Repository,
    GIT_MERGE_ANALYSIS_UP_TO_DATE,
    GIT_MERGE_ANALYSIS_FASTFORWARD,
    GIT_MERGE_ANALYSIS_NORMAL
)
from cookiecutter.main import cookiecutter
from battenberg.errors import (
    MergeConflictException,
    TemplateConflictException,
    TemplateNotFoundException
)
from battenberg.temporary_worktree import TemporaryWorktree


WORKTREE_NAME = 'templating'
TEMPLATE_BRANCH = 'template'
logger = logging.getLogger(__name__)


class Battenberg:

    def __init__(self, repo: Repository):
        self.repo = repo

    def is_installed(self) -> bool:
        return TEMPLATE_BRANCH in self.repo.listall_branches()

    def get_context(self, context_file: str) -> Dict[str, Any]:
        with open(os.path.join(self.repo.workdir, context_file)) as f:
            return json.load(f)

    def merge_template_branch(self, message: str, merge_target: str = None):
        branch = self.repo.lookup_branch(TEMPLATE_BRANCH)

        merge_target_ref = 'HEAD'
        if merge_target is not None:
            # If we have a merge target, ensure we have that branch and have switched to it
            # before continuing with merging.
            merge_target_ref = f'refs/heads/{merge_target}'
            if merge_target not in self.repo.branches:
                self.repo.branches.local.create(merge_target, self.repo.get(self.repo.head.target))
            self.repo.checkout(merge_target_ref)

        analysis, _ = self.repo.merge_analysis(branch.target, merge_target_ref)

        if analysis & GIT_MERGE_ANALYSIS_UP_TO_DATE:
            logger.info('The branch is already up to date, no need to merge.')

        elif analysis & GIT_MERGE_ANALYSIS_FASTFORWARD or analysis & GIT_MERGE_ANALYSIS_NORMAL:

            # Ensure we're merging into the right
            self.repo.checkout(merge_target_ref)

            # Let's merge template changes using --allow-unrelated-histories. This will allow
            # the disjoint histories to be merged successfully. If you want to manually replicate
            # this option please run:
            #
            #     "git merge --allow-unrelated-histories template"
            #
            self.repo.merge(branch.target)

            # If there is a conflict we should error and let the user manually resolve it.
            if self.repo.index.conflicts is not None:
                raise MergeConflictException(
                    f'Cannot merge the template commit ({branch.target}) with the current HEAD '
                    f'({self.repo.head}). Please resolve them manually and run \'git commit\' '
                    'to merge'
                )

            # Stage all the changes for commit.
            tree = self.repo.index.write_tree()

            # Add the commit back to the HEAD (normally the master branch unless --merge-target
            # is passed).
            self.repo.create_commit(
                'HEAD',
                self.repo.default_signature,
                self.repo.default_signature,
                message,
                tree,
                [self.repo.head.target, branch.target]
            )

            # Ensure we're not keeping any lingering metadata state before trying to merge the tmp
            # worktree into the main HEAD.
            self.repo.state_cleanup()
            self.repo.checkout('HEAD')
        else:
            raise AssertionError(f'Unknown merge analysis result: {analysis}')

    def install(self, template: str, checkout: str = 'master', extra_context: Dict = None,
                no_input: bool = False):
        if extra_context is None:
            extra_context = {}

        # Assert template branch doesn't exist or raise conflict
        if self.is_installed():
            raise TemplateConflictException()

        # Create temporary worktree
        with TemporaryWorktree(self.repo, WORKTREE_NAME) as worktree:
            cookiecutter(
                template,
                checkout,
                no_input,
                extra_context=extra_context,
                replay=False,
                overwrite_if_exists=True,
                output_dir=worktree.path,
                strip=True
            )

            # Stage changes
            worktree.repo.index.add_all()
            worktree.repo.index.write()
            tree = worktree.repo.index.write_tree()

            # Create an orphaned commit
            oid = worktree.repo.create_commit(
                None,
                worktree.repo.default_signature,
                worktree.repo.default_signature,
                'Prepared template installation',
                tree,
                []
            )
            commit = self.repo.get(oid)

            # Create a branch which target orphaned commit
            branch = self.repo.create_branch(TEMPLATE_BRANCH, commit)

            # Optionally, set worktree HEAD to this branch (useful for debugging)
            # Optional ? Obviously the tmp worktree will be removed in __exit__
            worktree.repo.set_head(branch.name)

        # Let's merge our changes into HEAD
        self.merge_template_branch(f'Installed template \'{template}\'')

    def upgrade(self, checkout: str = 'master', extra_context: Dict = None, no_input: bool = False,
                merge_target: str = None, context_file: str = '.cookiecutter.json'):
        if extra_context is None:
            extra_context = {}

        # Assert template branch exist or raise an error
        if not self.is_installed():
            raise TemplateNotFoundException()

        # Get last context used to apply template
        context = self.get_context(context_file)
        logger.debug(f'Found context: {context}')
        # Fetch template information, this is normally the git:// URL.
        template = context['_template']
        logger.debug(f'Found template: {template}')

        # Merge original context and extra_context (priority to extra_context)
        context.update(extra_context)
        logger.debug(f'Context incl. extra: {context}')

        # Create temporary EMPTY worktree
        with TemporaryWorktree(self.repo, WORKTREE_NAME) as worktree:
            # Set HEAD to template branch
            branch = worktree.repo.lookup_branch(TEMPLATE_BRANCH)
            worktree.repo.set_head(branch.name)

            cookiecutter(
                template,
                checkout,
                no_input,
                extra_context=context,
                replay=False,
                overwrite_if_exists=True,
                output_dir=worktree.path,
                strip=True
            )

            # Stage changes
            worktree.repo.index.read()
            worktree.repo.index.add_all()
            worktree.repo.index.write()
            tree = worktree.repo.index.write_tree()

            # Create commit on the template branch
            oid = worktree.repo.create_commit(
                'HEAD',
                worktree.repo.default_signature,
                worktree.repo.default_signature,
                'Prepared template upgrade',
                tree,
                [worktree.repo.head.target]
            )
            commit = worktree.repo.get(oid)

        # Make template branch ref to created commit
        self.repo.lookup_branch(TEMPLATE_BRANCH).set_target(commit.hex)

        # Let's merge our changes into HEAD
        self.merge_template_branch(f'Upgraded template \'{template}\'', merge_target)
