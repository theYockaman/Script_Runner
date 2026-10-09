FROM nginx:stable-alpine
COPY index.html logo.svg favicon.ico apple-touch-icon.png /usr/share/nginx/html/
COPY default.conf /etc/nginx/conf.d/default.conf
RUN chown -R nginx:nginx /usr/share/nginx/html && chmod -R 755 /usr/share/nginx/html
EXPOSE 80
