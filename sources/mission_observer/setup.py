from setuptools import find_packages, setup


package_name = "mission_observer"


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
    description="Passive PX4 mission continuity observer (VehicleStatus stream).",
    license="Apache-2.0",
    entry_points={
        "console_scripts": [
            "mission_observer = mission_observer.node:main",
        ],
    },
)
