from setuptools import find_packages, setup


package_name = "companion_analytics"


setup(
    name=package_name,
    version="0.1.0",
    packages=find_packages(),
    data_files=[
        (
            "share/ament_index/resource_index/packages",
            [f"resource/{package_name}"],
        ),
        (f"share/{package_name}", ["package.xml"]),
    ],
    install_requires=["setuptools"],
    zip_safe=True,
    maintainer="Cloud Native ROS Kubernetes Project",
    maintainer_email="research@cloud-native-robotics.local",
    description="Lifecycle-managed analytics workload for P2.",
    license="Apache-2.0",
    entry_points={
        "console_scripts": [
            "companion_analytics = companion_analytics.node:main",
        ],
    },
)
