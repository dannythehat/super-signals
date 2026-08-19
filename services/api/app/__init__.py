"""Super Signals API package.

Temporary compatibility installs remain only for behaviours not yet folded into the
canonical runtime. The canonical cleanup removes these imports block by block; no new
runtime override may be added here.
"""

from app.performance_account_truth_override import install_performance_account_truth_override

install_performance_account_truth_override()

from app.account_truth_poll_override import install_account_truth_poll_override

install_account_truth_poll_override()

from app.aug18_readiness_cleanup import install_aug18_readiness_cleanup

install_aug18_readiness_cleanup()

from app.post_execution_edit_fidelity import install_post_execution_edit_fidelity

install_post_execution_edit_fidelity()
