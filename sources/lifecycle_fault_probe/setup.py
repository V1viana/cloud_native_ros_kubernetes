from setuptools import find_packages, setup


package_name = "lifecycle_fault_probe"


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
    description="Test fixture: a lifecycle node whose transitions fail on request.",
    license="Apache-2.0",
    entry_points={
        "console_scripts": [
            "lifecycle_fault_probe = lifecycle_fault_probe.node:main",
        ],
    },
)
