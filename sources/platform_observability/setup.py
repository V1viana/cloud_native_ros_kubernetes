from setuptools import find_packages, setup


package_name = "platform_observability"


setup(
    name=package_name,
    version="0.1.0",
    packages=find_packages(),
    data_files=[
        ("share/ament_index/resource_index/packages", [f"resource/{package_name}"]),
        (f"share/{package_name}", ["package.xml"]),
    ],
    install_requires=["setuptools"],
    zip_safe=True,
    maintainer="Cloud Native ROS Kubernetes Project",
    maintainer_email="research@cloud-native-robotics.local",
    description="Durable audit, notification and Kubernetes observation.",
    license="Apache-2.0",
    entry_points={
        "console_scripts": [
            "audit_writer = platform_observability.audit_writer:main",
            "operator_notifier = platform_observability.operator_notifier:main",
            "platform_observer = platform_observability.platform_observer:main",
        ],
    },
)
