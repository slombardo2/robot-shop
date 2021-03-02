import random
#import instana
import os
import sys
import datetime
import logging
import uuid
import json
import requests
import traceback
#import opentracing as ot
#import opentracing.ext.tags as tags
from flask import Flask
from flask import Response
from flask import request
from flask import render_template
from helpers import *
from flask import jsonify
from flask import g
from rabbitmq import Publisher

#Honeycomb
import beeline
from beeline.middleware.flask import HoneyMiddleware
from uwsgidecorators import postfork

# Pass your Flask app to HoneyMiddleware
app = Flask(__name__)
HoneyMiddleware(app, db_events=False) # db_events defaults to True, set to False if not using our db middleware with Flask-SQLAlchemy
app.logger.addHandler(logging.StreamHandler(sys.stderr))
app.logger.setLevel(logging.INFO)

# Prometheus
import prometheus_client
from prometheus_client import Counter, Histogram

CART = os.getenv('CART_HOST', 'cart')
USER = os.getenv('USER_HOST', 'user')
PAYMENT_GATEWAY = os.getenv('PAYMENT_GATEWAY', 'https://paypal.com/')

# Prometheus
PromMetrics = {}
PromMetrics['SOLD_COUNTER'] = Counter('sold_count', 'Running count of items sold')
PromMetrics['AUS'] = Histogram('units_sold', 'Avergae Unit Sale', buckets=(1, 2, 5, 10, 100))
PromMetrics['AVS'] = Histogram('cart_value', 'Avergae Value Sale', buckets=(100, 200, 500, 1000, 2000, 5000, 10000))

@postfork
def init_beeline():
    logging.info(f'beeline initialization in process pid {os.getpid()}')
    beeline.init(writekey="f9e0f7c58be2dde4c878162daed00123", dataset="rs-payment", debug=True)

@app.errorhandler(Exception)
def exception_handler(err):
    g.ev.add_field("errors.message", err.message)
    response = jsonify(err.to_dict())
    response.status_code = err.status_code
    return response
#    app.logger.error(str(err))
#    return str(err), 500

@app.before_request
def before():
    # g is the thread-local / request-local variable, we will use it to store
    # information that will be used when we eventually send the event to
    # Honeycomb, including a timer for the whole request duration.
    g.req_start = datetime.datetime.now()
   # g.ev = libhoney_builder.new_event()
    g.ev = beeline.new_event()
    g.ev.add_field("request.path", request.path)
    g.ev.add_field("request.method", request.method)
    g.ev.add_field("request.user_agent.browser", request.user_agent.browser)
    g.ev.add_field("request.user_agent.platform", request.user_agent.platform)
    g.ev.add_field("request.user_agent.language", request.user_agent.language)
    g.ev.add_field("request.user_agent.string", request.user_agent.string)
    g.ev.add_field("request.user_agent.version", request.user_agent.version)
    g.ev.add_field("request.python_function", request.endpoint)
    g.ev.add_field("request.url_pattern", str(request.url_rule))
    
@app.after_request
def after(response):
    g.ev.add_field("response.status_code", response.status_code)

    # Note that this isn"t the total time to serve the request, i.e., how long
    # the end user is waiting. It accounts for the time spent in the Flask
    # handlers but not Werkzeug, etc. Ingesting edge data from ELB or
    # nginx etc. is usually much better for that kind of (total request time) info.
    g.ev.add_field("timers.flask_time_ms", milliseconds_since(g.req_start))

    app.logger.debug(g.ev)
    g.ev.send()
    return response

@app.route('/health', methods=['GET'])
def health():
    return jsonify(up=True)
    #return 'OK'

# Prometheus
@app.route('/metrics', defaults={"metric_id": None}, methods=['GET'])
#@app.route('/metrics', methods=['GET'])
@app.route('/metrics/<int:metric_id>/', methods=['GET']
def metrics():
    res = []
    for m in PromMetrics.values():
        res.append(prometheus_client.generate_latest(m))

    return Response(res, mimetype='text/plain')


@app.route('/pay/<id>', methods=['POST'])
def pay(id):
    trace = beeline.start_trace(context={
        "name": "rs-payment",
        "hostname": hostname,
        "user_id": id
        # other initial context
    })
    app.logger.info('payment for {}'.format(id))
    cart = request.get_json()
    app.logger.info(cart)
    anonymous_user = True
    beeline.add_context({
        'id': id,
        'cart': cart
    )}

    # add some log info to the active trace
    #span = beeline.start_span(context={
    #span.log_kv({'id': id})
    #span.log_kv({'cart': cart})

    # check user exists
    try:
        req = requests.get('http://{user}:8080/check/{id}'.format(user=USER, id=id))
    except requests.exceptions.RequestException as err:
        app.logger.error(err)
        return str(err), 500
    if req.status_code == 200:
        anonymous_user = False
    beeline.add_context({
        'Request': req,
        'Error': err,
        'User': user
    )}

    # check that the cart is valid
    # this will blow up if the cart is not valid
    has_shipping = False
    for item in cart.get('items'):
        if item.get('sku') == 'SHIP':
            has_shipping = True

    if cart.get('total', 0) == 0 or has_shipping == False:
        app.logger.warn('cart not valid')
        return 'cart not valid', 400

    # dummy call to payment gateway, hope they dont object
    try:
        req = requests.get(PAYMENT_GATEWAY)
        app.logger.info('{} returned {}'.format(PAYMENT_GATEWAY, req.status_code))
    except requests.exceptions.RequestException as err:
        app.logger.error(err)
        return str(err), 500
    if req.status_code != 200:
        return 'payment error', req.status_code
    beeline.add_context({
        'Request': req,
        'Error': err,
        'User': user
    )}

    # Prometheus
    # items purchased
    item_count = countItems(cart.get('items', []))
    PromMetrics['SOLD_COUNTER'].inc(item_count)
    PromMetrics['AUS'].observe(item_count)
    PromMetrics['AVS'].observe(cart.get('total', 0))

    # Generate order id
    orderid = str(uuid.uuid4())
    queueOrder({ 'orderid': orderid, 'user': id, 'cart': cart })
    beeline.add_context({
        'QueueOrder': queueOrder,
        'Order#': orderid,
        'Cart': cart,
        'User': id
    )}

    # add to order history
    if not anonymous_user:
        try:
            req = requests.post('http://{user}:8080/order/{id}'.format(user=USER, id=id),
                    data=json.dumps({'orderid': orderid, 'cart': cart}),
                    headers={'Content-Type': 'application/json'})
            app.logger.info('order history returned {}'.format(req.status_code))
        except requests.exceptions.RequestException as err:
            app.logger.error(err)
            return str(err), 500
        beeline.add_context({
        'Request': req,
        'Order#': orderid,
        'Cart': cart,
        'User': user
        'Error': err,
    )}

    # delete cart
    try:
        req = requests.delete('http://{cart}:8080/cart/{id}'.format(cart=CART, id=id));
        app.logger.info('cart delete returned {}'.format(req.status_code))
    except requests.exceptions.RequestException as err:
        app.logger.error(err)
        return str(err), 500
    if req.status_code != 200:
        return 'order history update error', req.status_code

    return jsonify({ 'orderid': orderid })


def queueOrder(order):
    app.logger.info('queue order')
    # RabbitMQ pika is not currently traced automatically
    # opentracing tracer is automatically set to Instana tracer
    # start a span

    parent_span = ot.tracer.active_span
    with ot.tracer.start_active_span('queueOrder', child_of=parent_span,
            tags={
                    'exchange': Publisher.EXCHANGE,
                    'key': Publisher.ROUTING_KEY
                }) as tscope:
        tscope.span.set_tag('span.kind', 'intermediate')
        tscope.span.log_kv({'orderid': order.get('orderid')})
        with ot.tracer.start_active_span('rabbitmq', child_of=tscope.span,
                tags={
                    'exchange': Publisher.EXCHANGE,
                    'sort': 'publish',
                    'address': Publisher.HOST,
                    'key': Publisher.ROUTING_KEY
                    }
                ) as scope:

            # For screenshot demo requirements optionally add in a bit of delay
            delay = int(os.getenv('PAYMENT_DELAY_MS', 0))
            time.sleep(delay / 1000)

            headers = {}
            ot.tracer.inject(scope.span.context, ot.Format.HTTP_HEADERS, headers)
            app.logger.info('msg headers {}'.format(headers))

            publisher.publish(order, headers)


def countItems(items):
    count = 0
    for item in items:
        if item.get('sku') != 'SHIP':
            count += item.get('qty')

    return count

class InstanaDataCenterMiddleware():
    data_centers = [
        "us-east1",
        "us-east2",
        "us-east3",
        "us-east4",
        "us-central1",
        "us-west1",
        "us-west2",
        "eu-west3",
        "eu-west4"
    ]

    def __init__(self, app):
        self.app = app

    def __call__(self, environ, start_response):
        span = ot.tracer.active_span

        span.log_kv({'datacenter': random.choice(self.data_centers)})

        return self.app(environ, start_response)


# RabbitMQ
publisher = Publisher(app.logger)

if __name__ == "__main__":
    sh = logging.StreamHandler(sys.stdout)
    sh.setLevel(logging.INFO)
    fmt = logging.Formatter('%(asctime)s - %(name)s - %(levelname)s - %(message)s')
    app.logger.info('Payment gateway {}'.format(PAYMENT_GATEWAY))
    port = int(os.getenv("SHOP_PAYMENT_PORT", "8080"))
    app.logger.info('Starting on port {}'.format(port))
    app.wsgi_app = InstanaDataCenterMiddleware(app.wsgi_app)
    app.run(host='0.0.0.0', port=port)
