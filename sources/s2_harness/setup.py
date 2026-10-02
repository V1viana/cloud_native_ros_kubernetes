from setuptools import find_packages, setup


package_name = "s2_harness"


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
    description="S2 test harness: local metrics recorder and transient injector (R11).",
    license="Apache-2.0",
    entry_points={
        "console_scripts": [
            "s2_harness = s2_harness.node:main",
            "s2_health_prober = s2_harness.prober:main",
        ],
    },
)
