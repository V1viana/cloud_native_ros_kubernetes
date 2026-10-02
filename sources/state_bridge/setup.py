from setuptools import find_packages, setup


package_name = "state_bridge"


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
    description="Mirrors a ROS 2 Lifecycle node's state into its ROSModule custom resource.",
    license="Apache-2.0",
    entry_points={
        "console_scripts": [
            "state_bridge = state_bridge.bridge:main",
        ],
    },
)
