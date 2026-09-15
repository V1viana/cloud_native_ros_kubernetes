from setuptools import find_packages, setup


package_name = "e0_kuberos_bootstrap"


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
    install_requires=["setuptools", "PyYAML"],
    zip_safe=True,
    maintainer="Cloud Native ROS Kubernetes Project",
    maintainer_email="research@cloud-native-robotics.local",
    description="Bootstrap E0 ApplicationDeployments through KubeROS.",
    license="Apache-2.0",
    entry_points={
        "console_scripts": [
            "e0_kuberos_bootstrap = e0_kuberos_bootstrap.bootstrap:main",
            "e0_kuberos_update = e0_kuberos_bootstrap.update:main",
        ],
    },
)
