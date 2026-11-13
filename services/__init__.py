"""DataSentry services.

One package per deployable unit. They share the packages/ libraries and talk
to each other only through the event bus and the control plane, never by
importing each other -- which is what keeps them separately deployable and
separately scalable (docs/02 section 3).
"""
