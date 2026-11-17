# The lab's own image: simulated devices (snmpsim plus their syslog, traps and flows), the lab
# LibreNMS, and the end-to-end tests. Nothing in it goes to an event.
FROM python:3.11-slim
# net-snmp's tools: the lab LibreNMS walks with snmpbulkwalk, the devices send traps with snmptrap.
RUN apt-get update \
    && apt-get install -y --no-install-recommends snmp \
    && rm -rf /var/lib/apt/lists/*
# snmpsim answers SNMP from the .snmprec files; pytest and requests run the end-to-end tests.
# snmpsim imports pysmi without declaring it.
RUN pip install --no-cache-dir "snmpsim==1.2.2" "pysmi==2.0.0" "pytest==8.4.2" "requests==2.32.5"
WORKDIR /lab
COPY . /lab
RUN useradd --system --uid 10002 --no-create-home --shell /usr/sbin/nologin lab
# No cache folder: /lab belongs to root and the tests run as lab.
CMD ["python", "-m", "pytest", "-q", "-p", "no:cacheprovider", "/lab/e2e"]
