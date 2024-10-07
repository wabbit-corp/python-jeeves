import logging
import importlib
import sys, os, time

###################################################################################################
# Installation
###################################################################################################

def _can_import_module(module_name: str) -> bool:
    try:
        importlib.import_module(module_name)
        return True
    except ImportError:
        return False

def install_package(*, pip_package_name: str, module_name: str) -> bool:
    logger = logging.getLogger(__name__)

    # if optional:
    #     if os.path.exists('./.dont_install_{pip_package_name}'):
    #         return False

    if not _can_import_module(module_name):
        # if optional:
        #     logger.warning(f'Optional package {pip_package_name} not found.')
        # else:
        logger.error(f'Required package {pip_package_name} not found!')

        logger.info(f'Running: pip3 install --user {pip_package_name}')
        if os.system(f'pip3 install --user {pip_package_name}') != 0:
            logger.error(f'Failed to install {pip_package_name}!')
            return False

        return True

        # print_question(f'Would you like to install it now? [y/n]')
        # response = input().strip().lower()
        # if response == 'y':
        #     print_info(f'Running: pip3 install --user {pip_package_name}. Press enter to continue (or Ctrl+C to abort).')
        #     response = input().strip()
        #     if response != '':
        #         print_failure(f'Aborting!')
        #         sys.exit(1)

        #     if os.system(f'pip3 install {pip_package_name}') != 0:
        #         print_failure(f'Failed to install {pip_package_name}!')
        #         sys.exit(1)

        #     print_success(f'Successfully installed {pip_package_name}!')
        #     return True
        # else:
        #     if optional:
        #         print_info(f'Skipping optional package {pip_package_name}')
        #         with open('./data/.dont_install_{pip_name}', 'wt') as f:
        #             f.write('')
        #     else:
        #         print_failure(f'A required package {pip_package_name} is not installed. Please install it and try again.')
        #         sys.exit(1)

        #     return False

    return False
