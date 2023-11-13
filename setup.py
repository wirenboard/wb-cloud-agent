from setuptools import setup

setup(
    name='wb_cloud_agent',
    version='0.1.0',
    packages=['wb_cloud_agent'],
    entry_points={
        'console_scripts': ['wb-cloud-agent = wb_cloud_agent.wb_cloud_agent:main']
    },
    python_requires='>=3.9.0',
    install_requires=['requests'],
    url='https://wirenboard.cloud',
    license='',
    author='Alexey Chudin',
    author_email='kazqvaizer@fands.dev',
    description=''
)
