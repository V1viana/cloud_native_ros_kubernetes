from setuptools import find_packages, setup


package_name = "e1_battery_fault_harness"


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
    description="Typed low-battery fault and local safety observer for E1.",
    license="Apache-2.0",
    entry_points={
        "console_scripts": [
            "battery_fault_harness = "
            "e1_battery_fault_harness.battery_fault_harness:main",
        ],
    },
)
