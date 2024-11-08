import os
import json
import time
import logging
import requests
import shutil
from pathlib import Path
from dataclasses import dataclass
from typing import Optional, Dict, Any
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry
import git

@dataclass
class ArtifactMetadata:
    buildTime: str
    gitCommit: str
    branch: str
    workflow: str

@dataclass
class Version:
    version: str
    metadata: Optional[ArtifactMetadata] = None

@dataclass
class UpdateData:
    latestSHA: str
    artifactUrl: str
    updateType: str
    metadata: Optional[ArtifactMetadata] = None

@dataclass
class UpdateResponse:
    status: bool
    message: str
    data: Optional[UpdateData]

    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> 'UpdateResponse':
        if 'data' in data and data['data']:
            metadata = None
            if 'metadata' in data['data']:
                metadata = ArtifactMetadata(
                    buildTime=data['data']['metadata'].get('buildTime', ''),
                    gitCommit=data['data']['metadata'].get('gitCommit', ''),
                    branch=data['data']['metadata'].get('branch', ''),
                    workflow=data['data']['metadata'].get('workflow', '')
                )
            return cls(
                status=data['status'],
                message=data['message'],
                data=UpdateData(
                    latestSHA=data['data']['latestSHA'],
                    artifactUrl=data['data']['artifactUrl'],
                    updateType=data['data']['updateType'],
                    metadata=metadata
                )
            )
        return cls(
            status=data['status'],
            message=data['message'],
            data=None
        )

class ComponentUpdateClient:
    VERSION_FILE = "/opt/ota-client/current_version.json"
    UPDATE_CHECK_INTERVAL = 300  # 5 minutes
    COMPONENT_PATH = "/opt/ota-client/components"

    def __init__(self):
        self.device_id = os.getenv("DEVICE_ID", "device-100")
        self.api_base_url = os.getenv("API_URL", "http://13.232.234.162:5000/api")
        self.component_name = os.getenv("COMPONENT_NAME", "oro_git_ws")
        
        # Ensure required directories exist
        os.makedirs(os.path.dirname(self.VERSION_FILE), exist_ok=True)
        os.makedirs(self.COMPONENT_PATH, exist_ok=True)
        
        # Setup logging
        logging.basicConfig(
            level=logging.INFO,
            format='%(asctime)s - %(name)s - %(levelname)s - %(message)s',
            handlers=[
                logging.FileHandler("/var/log/ota-client.log"),
                logging.StreamHandler()
            ]
        )
        self.logger = logging.getLogger("ComponentUpdateClient")
        
        # Setup requests session with retry strategy
        self.session = requests.Session()
        retry_strategy = Retry(
            total=3,
            backoff_factor=1,
            status_forcelist=[429, 500, 502, 503, 504],
        )
        adapter = HTTPAdapter(max_retries=retry_strategy)
        self.session.mount("http://", adapter)
        self.session.mount("https://", adapter)
        
        # Initialize version
        self.current_version = self._load_current_version()

    def _load_current_version(self) -> Version:
        try:
            if os.path.exists(self.VERSION_FILE):
                with open(self.VERSION_FILE, 'r') as f:
                    data = json.load(f)
                    metadata = None
                    if 'metadata' in data:
                        metadata = ArtifactMetadata(
                            buildTime=data['metadata'].get('buildTime', ''),
                            gitCommit=data['metadata'].get('gitCommit', ''),
                            branch=data['metadata'].get('branch', ''),
                            workflow=data['metadata'].get('workflow', '')
                        )
                    return Version(version=data.get('version', 'unknown'), metadata=metadata)
            else:
                default_version = Version(version='unknown')
                self._save_current_version(default_version)
                return default_version
        except Exception as e:
            self.logger.error(f"Error loading version file: {e}")
            return Version(version='unknown')

    def _save_current_version(self, version: Version) -> None:
        try:
            data = {
                'version': version.version,
                'metadata': {
                    'buildTime': version.metadata.buildTime,
                    'gitCommit': version.metadata.gitCommit,
                    'branch': version.metadata.branch,
                    'workflow': version.metadata.workflow
                } if version.metadata else None
            }
            with open(self.VERSION_FILE, 'w') as f:
                json.dump(data, f, indent=2)
        except Exception as e:
            self.logger.error(f"Error saving version file: {e}")

    def _apply_component_update(self, update_info: UpdateResponse) -> None:
        try:
            if not update_info.data:
                self.logger.error("Update info contains no data")
                return

            # Prepare the component directory
            component_dir = Path(self.COMPONENT_PATH) / self.component_name
            if component_dir.exists():
                shutil.rmtree(component_dir)

            # Clone the repository
            self.logger.info(f"Cloning repository from {update_info.data.artifactUrl}")
            repo = git.Repo.clone_from(
                update_info.data.artifactUrl,
                component_dir,
                branch='main'
            )

            # Checkout the specific commit
            if update_info.data.latestSHA:
                repo.git.checkout(update_info.data.latestSHA)

            # Update version information
            new_version = Version(
                version=update_info.data.latestSHA,
                metadata=update_info.data.metadata
            )
            self.current_version = new_version
            self._save_current_version(new_version)

            # Run post-update scripts if they exist
            post_update_script = component_dir / "scripts" / "post_update.sh"
            if post_update_script.exists():
                self.logger.info("Running post-update script")
                os.chmod(post_update_script, 0o755)
                result = os.system(str(post_update_script))
                if result != 0:
                    raise Exception(f"Post-update script failed with exit code {result}")

            self.logger.info("Component update completed successfully")

        except Exception as e:
            self.logger.error(f"Failed to apply component update: {e}")
            raise

    def check_for_updates(self) -> None:
        try:
            url = f"{self.api_base_url}/checkForUpdate/{self.device_id}/ota_update"
            self.logger.info(f"Checking for updates: {url}")
            
            response = self.session.get(url, timeout=(5, 15))
            response.raise_for_status()
            
            update_info = UpdateResponse.from_dict(response.json())
            
            if update_info.status and update_info.data:
                self.logger.info(f"Latest SHA: {update_info.data.latestSHA}")
                self.logger.info(f"Current version: {self.current_version.version}")
                
                if update_info.data.latestSHA != self.current_version.version:
                    self.logger.info("Update available!")
                    if update_info.data.updateType == "component":
                        self._apply_component_update(update_info)
                    else:
                        self.logger.info(f"Ignoring update type: {update_info.data.updateType}")
                else:
                    self.logger.info("Already on latest version")
            else:
                self.logger.info(f"No updates available: {update_info.message}")
                
        except Exception as e:
            self.logger.error(f"Error checking for updates: {e}")
        finally:
            time.sleep(self.UPDATE_CHECK_INTERVAL)

    def run(self) -> None:
        self.logger.info("Starting OTA component update client...")
        self.logger.info(f"Device ID: {self.device_id}")
        self.logger.info(f"Component: {self.component_name}")
        self.logger.info(f"Current version: {self.current_version.version}")
        
        while True:
            try:
                self.check_for_updates()
            except Exception as e:
                self.logger.error(f"Unexpected error in main loop: {e}")
                time.sleep(60)

if __name__ == "__main__":
    client = ComponentUpdateClient()
    client.run()