from glob import glob
from setuptools import find_packages, setup


package_name = "cloud_native_application_manager"


setup(
    name=package_name,
    version="0.1.0",
    packages=find_packages(),
    data_files=[
        ("share/ament_index/resource_index/packages", [f"resource/{package_name}"]),
        (f"share/{package_name}", ["package.xml"]),
        (f"share/{package_name}/config", glob("config/*.yaml")),
    ],
    install_requires=["setuptools"],
    zip_safe=True,
    maintainer="Cloud Native ROS Kubernetes Project",
    maintainer_email="research@cloud-native-robotics.local",
    description="Fleet-level policy control loop and KubeROS adapter.",
    license="Apache-2.0",
    entry_points={
        "console_scripts": [
            "application_manager = cloud_native_application_manager.node:main",
        ],
    },
)
