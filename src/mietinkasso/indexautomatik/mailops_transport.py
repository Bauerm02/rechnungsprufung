"""Map the existing index transport to the private, authenticated MailOps API."""
from mietinkasso.indexautomatik.mailops_client import MailOpsAuftrag, MailOpsClient


class MailOpsTransportadapter:
    def __init__(self, client: MailOpsClient):
        self.client = client

    def senden(self, auftrag):
        return self.client.senden(MailOpsAuftrag(
            referenz=auftrag.referenz, art="INDEX", empfaenger_email=auftrag.empfaenger_email or "",
            empfaenger_name=auftrag.empfaenger_name, betreff=auftrag.betreff, text=auftrag.text,
            freigabe_referenz=auftrag.freigabe_referenz,
        ))

    def status_abfragen(self, referenz):
        return self.client.status_abfragen(referenz)
