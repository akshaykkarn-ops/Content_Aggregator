from flask import Flask, render_template, request, redirect, url_for, jsonify, flash
from flask_sqlalchemy import SQLAlchemy
from flask_migrate import Migrate
from datetime import datetime
import requests
from bs4 import BeautifulSoup
from urllib.parse import urljoin, urlparse
import feedparser
import time
import threading
from apscheduler.schedulers.background import BackgroundScheduler
import re
import os

app = Flask(__name__)
app.config['SECRET_KEY'] = 'your-secret-key-here'
app.config['SQLALCHEMY_DATABASE_URI'] = 'sqlite:///content_aggregator.db'
app.config['SQLALCHEMY_TRACK_MODIFICATIONS'] = False

db = SQLAlchemy(app)
migrate = Migrate(app, db)

# Database Models
class Keyword(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    term = db.Column(db.String(100), unique=True, nullable=False)
    active = db.Column(db.Boolean, default=True)
    created_at = db.Column(db.DateTime, default=datetime.utcnow)
    
    def __repr__(self):
        return f'<Keyword {self.term}>'

class Source(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    name = db.Column(db.String(100), nullable=False)
    url = db.Column(db.String(500), nullable=False)
    source_type = db.Column(db.String(50), default='website') # website, rss
    active = db.Column(db.Boolean, default=True)
    last_scraped = db.Column(db.DateTime)
    created_at = db.Column(db.DateTime, default=datetime.utcnow)
    
    def __repr__(self):
        return f'<Source {self.name}>'

class Article(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    title = db.Column(db.String(500), nullable=False)
    content = db.Column(db.Text)
    summary = db.Column(db.Text)
    url = db.Column(db.String(1000), unique=True, nullable=False)
    author = db.Column(db.String(200))
    published_date = db.Column(db.DateTime)
    scraped_date = db.Column(db.DateTime, default=datetime.utcnow)
    source_id = db.Column(db.Integer, db.ForeignKey('source.id'))
    
    source = db.relationship('Source', backref=db.backref('articles', lazy=True))
    
    def __repr__(self):
        return f'<Article {self.title[:50]}>'

class ArticleKeyword(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    article_id = db.Column(db.Integer, db.ForeignKey('article.id'), nullable=False)
    keyword_id = db.Column(db.Integer, db.ForeignKey('keyword.id'), nullable=False)
    relevance_score = db.Column(db.Float, default=0.0)
    
    article = db.relationship('Article', backref=db.backref('keywords', lazy=True))
    keyword = db.relationship('Keyword', backref=db.backref('articles', lazy=True))

# Content Scraper Classes
class ContentScraper:
    def __init__(self):
        self.session = requests.Session()
        self.session.headers.update({
            'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/91.0.4472.124 Safari/537.36'
        })
    
    def scrape_website(self, url, keywords):
        """Scrape a website for content matching keywords"""
        try:
            response = self.session.get(url, timeout=10)
            response.raise_for_status()
            
            soup = BeautifulSoup(response.content, 'html.parser')
            
            # Remove script and style elements
            for script in soup(["script", "style"]):
                script.decompose()
            
            # Extract text content
            text_content = soup.get_text()
            title = soup.find('title').get_text() if soup.find('title') else url
            
            # Check if content matches keywords
            keyword_matches = self.check_keyword_matches(text_content, keywords)
            
            if keyword_matches:
                # Try to extract article content more specifically
                article_content = self.extract_article_content(soup)
                
                return {
                    'title': title.strip(),
                    'content': article_content[:5000], # Limit content length
                    'url': url,
                    'keywords': keyword_matches
                }
                
        except Exception as e:
            print(f"Error scraping {url}: {str(e)}")
            return None
    
    def scrape_rss(self, rss_url, keywords):
        """Scrape RSS feed for articles matching keywords"""
        try:
            feed = feedparser.parse(rss_url)
            articles = []
            
            for entry in feed.entries[:10]: # Limit to 10 most recent entries
                title = entry.get('title', '')
                content = entry.get('description', '') + ' ' + entry.get('summary', '')
                
                keyword_matches = self.check_keyword_matches(title + ' ' + content, keywords)
                
                if keyword_matches:
                    articles.append({
                        'title': title,
                        'content': content[:5000],
                        'url': entry.get('link', ''),
                        'author': entry.get('author', ''),
                        'published_date': self.parse_date(entry.get('published')),
                        'keywords': keyword_matches
                    })
            
            return articles
            
        except Exception as e:
            print(f"Error scraping RSS {rss_url}: {str(e)}")
            return []
    
    def extract_article_content(self, soup):
        """Extract main article content from HTML"""
        # Try common article selectors
        article_selectors = [
            'article', '[role="main"]', '.article-content', 
            '.post-content', '#content', '.entry-content',
            '.story-body', '.article-body'
        ]
        
        for selector in article_selectors:
            element = soup.select_one(selector)
            if element:
                return element.get_text(strip=True)
        
        # Fallback to body content
        body = soup.find('body')
        return body.get_text(strip=True) if body else soup.get_text(strip=True)
    
    def check_keyword_matches(self, content, keywords):
        """Check which keywords match in the content"""
        matches = []
        content_lower = content.lower()
        
        for keyword in keywords:
            if keyword.term.lower() in content_lower:
                # Calculate simple relevance score
                count = content_lower.count(keyword.term.lower())
                score = min(count / 10.0, 1.0) # Normalize to 0-1
                matches.append({'keyword': keyword, 'score': score})
        
        return matches
    
    def parse_date(self, date_string):
        """Parse date string to datetime object"""
        if not date_string:
            return None
        
        try:
            import email.utils
            return datetime.fromtimestamp(time.mktime(email.utils.parsedate(date_string)))
        except:
            return None

# Background scraping job
def run_content_scraping():
    """Background job to scrape content from all sources"""
    with app.app_context():
        scraper = ContentScraper()
        sources = Source.query.filter_by(active=True).all()
        keywords = Keyword.query.filter_by(active=True).all()
        
        for source in sources:
            print(f"Scraping source: {source.name}")
            
            try:
                if source.source_type == 'rss':
                    articles_data = scraper.scrape_rss(source.url, keywords)
                    
                    for article_data in articles_data:
                        # Check if article already exists
                        existing_article = Article.query.filter_by(url=article_data['url']).first()
                        if not existing_article:
                            article = Article(
                                title=article_data['title'],
                                content=article_data['content'],
                                url=article_data['url'],
                                author=article_data.get('author'),
                                published_date=article_data.get('published_date'),
                                source_id=source.id
                            )
                            
                            db.session.add(article)
                            db.session.flush() # To get article ID
                            
                            # Add keyword associations
                            for keyword_match in article_data['keywords']:
                                article_keyword = ArticleKeyword(
                                    article_id=article.id,
                                    keyword_id=keyword_match['keyword'].id,
                                    relevance_score=keyword_match['score']
                                )
                                db.session.add(article_keyword)
                
                else: # website scraping
                    article_data = scraper.scrape_website(source.url, keywords)
                    
                    if article_data:
                        # Check if article already exists
                        existing_article = Article.query.filter_by(url=article_data['url']).first()
                        if not existing_article:
                            article = Article(
                                title=article_data['title'],
                                content=article_data['content'],
                                url=article_data['url'],
                                source_id=source.id
                            )
                            
                            db.session.add(article)
                            db.session.flush()
                            
                            # Add keyword associations
                            for keyword_match in article_data['keywords']:
                                article_keyword = ArticleKeyword(
                                    article_id=article.id,
                                    keyword_id=keyword_match['keyword'].id,
                                    relevance_score=keyword_match['score']
                                )
                                db.session.add(article_keyword)
                
                source.last_scraped = datetime.utcnow()
                db.session.commit()
                
            except Exception as e:
                print(f"Error processing source {source.name}: {str(e)}")
                db.session.rollback()
            
            # Small delay between sources
            time.sleep(2)

# Routes
@app.route('/')
def index():
    """Main dashboard showing personalized content feed"""
    # Get articles ordered by relevance and date
    page = request.args.get('page', 1, type=int)
    keyword_filter = request.args.get('keyword', '', type=str)
    
    query = db.session.query(Article).join(ArticleKeyword).join(Keyword)
    
    if keyword_filter:
        query = query.filter(Keyword.term.ilike(f'%{keyword_filter}%'))
    
    articles = query.order_by(
        ArticleKeyword.relevance_score.desc(),
        Article.scraped_date.desc()
    ).paginate(
        page=page, per_page=20, error_out=False
    )
    
    keywords = Keyword.query.filter_by(active=True).all()
    
    return render_template('index.html', articles=articles, keywords=keywords, keyword_filter=keyword_filter)

@app.route('/keywords')
def manage_keywords():
    """Manage keywords for content filtering"""
    keywords = Keyword.query.all()
    return render_template('keywords.html', keywords=keywords)

@app.route('/keywords/add', methods=['POST'])
def add_keyword():
    """Add a new keyword"""
    term = request.form.get('term', '').strip()
    
    if term:
        existing_keyword = Keyword.query.filter_by(term=term.lower()).first()
        if not existing_keyword:
            keyword = Keyword(term=term.lower())
            db.session.add(keyword)
            db.session.commit()
            flash(f'Keyword "{term}" added successfully!', 'success')
        else:
            flash(f'Keyword "{term}" already exists!', 'warning')
    
    return redirect(url_for('manage_keywords'))

@app.route('/keywords/toggle/<int:keyword_id>')
def toggle_keyword(keyword_id):
    """Toggle keyword active status"""
    keyword = Keyword.query.get_or_404(keyword_id)
    keyword.active = not keyword.active
    db.session.commit()
    
    status = 'activated' if keyword.active else 'deactivated'
    flash(f'Keyword "{keyword.term}" {status}!', 'success')
    
    return redirect(url_for('manage_keywords'))

@app.route('/sources')
def manage_sources():
    """Manage content sources"""
    sources = Source.query.all()
    return render_template('sources.html', sources=sources)

@app.route('/sources/add', methods=['POST'])
def add_source():
    """Add a new content source"""
    name = request.form.get('name', '').strip()
    url = request.form.get('url', '').strip()
    source_type = request.form.get('source_type', 'website')
    
    if name and url:
        existing_source = Source.query.filter_by(url=url).first()
        if not existing_source:
            source = Source(name=name, url=url, source_type=source_type)
            db.session.add(source)
            db.session.commit()
            flash(f'Source "{name}" added successfully!', 'success')
        else:
            flash(f'Source with URL "{url}" already exists!', 'warning')
    
    return redirect(url_for('manage_sources'))

@app.route('/sources/toggle/<int:source_id>')
def toggle_source(source_id):
    """Toggle source active status"""
    source = Source.query.get_or_404(source_id)
    source.active = not source.active
    db.session.commit()
    
    status = 'activated' if source.active else 'deactivated'
    flash(f'Source "{source.name}" {status}!', 'success')
    
    return redirect(url_for('manage_sources'))

@app.route('/scrape/manual')
def manual_scrape():
    """Manually trigger content scraping"""
    # Run scraping in background thread
    thread = threading.Thread(target=run_content_scraping)
    thread.daemon = True
    thread.start()
    
    flash('Manual scraping started! Check back in a few minutes for new content.', 'info')
    return redirect(url_for('index'))

@app.route('/article/<int:article_id>')
def view_article(article_id):
    """View full article details"""
    article = Article.query.get_or_404(article_id)
    return render_template('article.html', article=article)

@app.route('/api/stats')
def api_stats():
    """API endpoint for dashboard statistics"""
    stats = {
        'total_articles': Article.query.count(),
        'total_keywords': Keyword.query.filter_by(active=True).count(),
        'total_sources': Source.query.filter_by(active=True).count(),
        'recent_articles': Article.query.filter(
            Article.scraped_date >= datetime.utcnow().replace(hour=0, minute=0, second=0)
        ).count()
    }
    return jsonify(stats)

# Initialize scheduler for background scraping
def init_scheduler():
    scheduler = BackgroundScheduler()
    # Schedule scraping every 4 hours
    scheduler.add_job(
        func=run_content_scraping,
        trigger="interval",
        hours=4,
        id='content_scraping_job'
    )
    scheduler.start()

if __name__ == '__main__':
    with app.app_context():
        db.create_all()
        
        # Add some default data if tables are empty
        if not Keyword.query.first():
            default_keywords = ['artificial intelligence', 'machine learning', 'technology', 'programming', 'python']
            for term in default_keywords:
                keyword = Keyword(term=term)
                db.session.add(keyword)
            
            default_sources = [
                {'name': 'Hacker News RSS', 'url': 'https://hnrss.org/frontpage', 'type': 'rss'},
                {'name': 'Reddit Technology RSS', 'url': 'https://www.reddit.com/r/technology/.rss', 'type': 'rss'},
                {'name': 'ArXiv CS RSS', 'url': 'https://rss.arxiv.org/rss/cs', 'type': 'rss'},
            ]
            
            for source_data in default_sources:
                source = Source(
                    name=source_data['name'],
                    url=source_data['url'],
                    source_type=source_data['type']
                )
                db.session.add(source)
            
            db.session.commit()
            print("Default data added to database.")
    
    # Initialize background scheduler
    init_scheduler()
    
    print("Starting Content Aggregator...")
    print("Visit http://127.0.0.1:5000 to access the application")
    app.run(debug=True, use_reloader=False) # use_reloader=False prevents duplicate scheduler
